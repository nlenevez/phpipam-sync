"""
ipamsync.plan

Turns "snapshot + current target state" into an ordered list of actions,
without performing any of them.

Separating planning from execution is the whole safety story of this
tool. The importer's default mode builds a plan and prints it; `--apply`
builds the *same* plan and then runs it. So a dry-run is not an
approximation of what would happen -- it is the actual decision, shown
before it is acted on. It also makes the interesting logic (matching by
natural key, field diffing, nesting order) unit-testable against a fake
client, which matters because the underlying client's write methods have
never been exercised against a live instance.

## Additive by default

By default this importer NEVER deletes on the target. Records that exist
on the target but not in the snapshot are emitted as `drift_*` actions:
reported, counted, never acted on. That keeps the target safe from a
truncated or mis-scoped snapshot -- the worst a bad snapshot can do is
add or update, not destroy. The cost is that the target accumulates
records the source has deleted, which the drift report makes visible so
an operator can clean up by hand.

## Deleting

`options.delete_drift` (or `--delete`) turns those `drift_*` reports into
real `delete_*` actions, making the replica a strict mirror. Three things
guard it, because this is the one operation here that can destroy data:

  1. It still requires `--apply`. Deletion is never a side effect of a
     dry run.
  2. It is scoped exactly like everything else -- only sections named in
     the config, and only subnets/addresses inside them. A record outside
     the replicated scope is not a candidate and never appears in a plan.
  3. A **safety limit** (`options.delete_limit_fraction`, default 0.1)
     refuses the whole run if it would delete more than that fraction of
     the in-scope target records. This is the important one. The failure
     that matters is not "one record wrongly deleted", it is a snapshot
     that is empty or scoped to the wrong section arriving over a link
     the replica cannot question, and taking the whole replica with it.
     A run that wants to delete 80% of your IPAM should stop and ask.
     `--force-delete` overrides it for the one run.

Deletion order is addresses first, then subnets deepest-first, so a
parent is never removed while a child still points at it.

## Matching is by natural key, always

Subnets match on (target section, canonical CIDR); addresses match on
(subnet, IP). Database ids are never carried across instances -- see
ipamsync.model for why.
"""

import ipaddress

from ipamsync import model
from ipamsync.snapshot import canonical_cidr

# Action kinds that change the target.
WRITE_KINDS = (
    "create_section",
    "create_subnet",
    "update_subnet",
    "create_address",
    "update_address",
    # Only ever emitted when deletion is explicitly enabled -- see
    # "Deleting" in this module's docstring.
    "delete_address",
    "delete_subnet",
)
# Action kinds that are reported only and never executed.
REPORT_KINDS = ("drift_subnet", "drift_address", "note")

#: Deletions are always allowed up to this many records regardless of the
#: fraction limit, so a small dataset is not permanently blocked by a
#: percentage that rounds to nothing.
DELETE_LIMIT_FLOOR = 5

#: Explanation attached to an orphan (a target record the snapshot does
#: not contain), keyed by whether deletion is enabled.
ORPHAN_REASON = {
    False: ("exists on target, absent from snapshot "
            "(not deleted -- deletion is disabled)"),
    True: "absent from snapshot -- deleting (strict mirror)",
}


class PlanError(Exception):
    """Raised when a plan cannot be built at all -- e.g. the snapshot
    names a target section that does not exist and may not be created."""


class Action:
    """One planned change, described by natural key rather than by id.

    `key` is the human-readable identity of the thing being acted on
    (a CIDR, or an IP within a CIDR); `detail` carries the fields to
    write, or the reason for a report-only action.
    """

    __slots__ = ("kind", "key", "detail", "section", "cidr", "ip")

    def __init__(self, kind, key, detail=None, *, section=None, cidr=None, ip=None):
        self.kind = kind
        self.key = key
        self.detail = detail or {}
        self.section = section
        self.cidr = cidr
        self.ip = ip

    @property
    def is_write(self):
        return self.kind in WRITE_KINDS

    def __repr__(self):
        return f"<Action {self.kind} {self.key}>"

    def describe(self):
        if self.kind in ("update_subnet", "update_address"):
            changes = ", ".join(
                f"{field}={value!r}" for field, value in sorted(self.detail.items())
            )
            return f"{self.kind:16} {self.key}  [{changes}]"
        if self.kind in ("drift_subnet", "drift_address", "note"):
            return f"{self.kind:16} {self.key}  -- {self.detail.get('reason', '')}"
        return f"{self.kind:16} {self.key}"


def order_documents(documents):
    """Orders subnet documents so a parent always precedes its children.

    phpIPAM nests subnets via masterSubnetId. The snapshot records the
    parent by CIDR (`master_subnet`), so at import time the parent must
    already exist on the target before the child can point at it.

    Documents whose parent is not part of this snapshot keep their
    relative order and are created top-level -- the importer notes that
    separately. A cycle (which phpIPAM should never produce) is broken
    rather than hung on, so a corrupt snapshot cannot deadlock the run.
    """
    by_cidr = {document["cidr"]: document for document in documents}
    ordered = []
    placed = set()
    visiting = set()

    def place(cidr):
        if cidr in placed or cidr not in by_cidr:
            return
        if cidr in visiting:
            # Cycle: stop descending; the caller's frame still appends.
            return
        visiting.add(cidr)
        parent = by_cidr[cidr].get("master_subnet")
        if parent:
            place(parent)
        visiting.discard(cidr)
        if cidr not in placed:
            placed.add(cidr)
            ordered.append(by_cidr[cidr])

    for document in sorted(documents, key=lambda d: d["cidr"]):
        place(document["cidr"])
    return ordered


def split_cidr(cidr):
    """'10.20.0.0/24' -> ('10.20.0.0', '24'). phpIPAM wants the network
    address and the prefix length as two separate fields on create."""
    network = ipaddress.ip_network(cidr, strict=False)
    return str(network.network_address), str(network.prefixlen)


def build_plan(documents, target, config):
    """Builds the ordered action list.

    `target` is a TargetView (see ipamsync.target) -- anything exposing
    section_by_name / subnets_by_cidr / addresses_by_ip works, which is
    how the tests drive this with a fake.
    """
    from ipamsync.config import target_section_name

    options = config["options"]
    deleting = bool(options.get("delete_drift"))
    actions = []
    # Collected separately from the create/update actions so they can be
    # counted against the safety limit and ordered (addresses first, then
    # subnets deepest-first) before being appended.
    orphan_addresses = []
    orphan_subnets = []

    documents = order_documents(documents)
    sections_needed = sorted({document["section"] for document in documents})

    # -- Sections: resolved once, by name ------------------------------
    section_ids = {}
    for source_section in sections_needed:
        wanted = target_section_name(config, source_section)
        existing = target.section_by_name(wanted)
        if existing:
            section_ids[source_section] = existing["id"]
            continue
        if not options.get("create_missing_sections"):
            raise PlanError(
                f"Target has no section named {wanted!r} (needed for source "
                f"section {source_section!r}). Create it on the target "
                f"instance, or map it to an existing one with "
                f"`section_map: {{{source_section}: <target name>}}`, or set "
                f"`options.create_missing_sections: true` to have this tool "
                f"create it. It is off by default because a section created "
                f"without permissions is invisible to every non-admin user."
            )
        actions.append(Action(
            "create_section", wanted,
            {"name": wanted}, section=source_section,
        ))
        section_ids[source_section] = None  # resolved during apply

    # -- Subnets and their addresses -----------------------------------
    snapshot_cidrs_by_section = {}
    for document in documents:
        source_section = document["section"]
        cidr = document["cidr"]
        snapshot_cidrs_by_section.setdefault(source_section, set()).add(cidr)

        section_id = section_ids[source_section]
        existing_subnets = (
            target.subnets_by_cidr(section_id) if section_id is not None else {}
        )
        current = existing_subnets.get(cidr)

        if current is None:
            actions.append(Action(
                "create_subnet", cidr,
                {
                    "fields": document["fields"],
                    "master_subnet": document.get("master_subnet"),
                },
                section=source_section, cidr=cidr,
            ))
        else:
            changed = model.diff_fields(document["fields"], current)
            if changed:
                actions.append(Action(
                    "update_subnet", cidr, changed,
                    section=source_section, cidr=cidr,
                ))

        # A VLAN on the source subnet is recorded in the snapshot but not
        # replicated (VLANs are out of the agreed scope). Surface it so
        # the omission is visible rather than silently mysterious.
        vlan = document.get("vlan")
        if vlan and vlan.get("number"):
            actions.append(Action(
                "note", f"{cidr} vlan {vlan['number']}",
                {"reason": "source subnet has a VLAN; VLANs are not replicated "
                           "-- set it on the target by hand if needed"},
                section=source_section, cidr=cidr,
            ))

        # -- Addresses --------------------------------------------------
        current_addresses = (
            target.addresses_by_ip(current["id"]) if current else {}
        )
        snapshot_ips = set()
        for entry in document["addresses"]:
            ip = entry["ip"]
            snapshot_ips.add(ip)
            present = current_addresses.get(ip)
            if present is None:
                actions.append(Action(
                    "create_address", f"{cidr} {ip}",
                    {"fields": entry["fields"]},
                    section=source_section, cidr=cidr, ip=ip,
                ))
            else:
                changed = model.diff_fields(entry["fields"], present)
                if changed:
                    actions.append(Action(
                        "update_address", f"{cidr} {ip}", changed,
                        section=source_section, cidr=cidr, ip=ip,
                    ))

        for ip in sorted(set(current_addresses) - snapshot_ips,
                         key=lambda value: str(value)):
            orphan_addresses.append(Action(
                "delete_address" if deleting else "drift_address",
                f"{cidr} {ip}",
                {"reason": ORPHAN_REASON[deleting]},
                section=source_section, cidr=cidr, ip=ip,
            ))

    # -- Subnet-level orphans, per section -----------------------------
    for source_section, cidrs in sorted(snapshot_cidrs_by_section.items()):
        section_id = section_ids[source_section]
        if section_id is None:
            continue  # freshly created section: nothing can be extra in it
        subnets_here = target.subnets_by_cidr(section_id)
        for cidr in sorted(set(subnets_here) - cidrs):
            orphan_subnets.append(Action(
                "delete_subnet" if deleting else "drift_subnet", cidr,
                {"reason": ORPHAN_REASON[deleting],
                 "prefixlen": _prefixlen(cidr)},
                section=source_section, cidr=cidr,
            ))
        if deleting:
            # Everything inside a subnet that is itself being deleted is
            # implicitly gone. Listing those addresses separately would
            # double-count them against the safety limit and issue
            # pointless API calls.
            doomed = {(a.section, a.cidr) for a in orphan_subnets}
            orphan_addresses = [a for a in orphan_addresses
                                if (a.section, a.cidr) not in doomed]

    if deleting:
        _check_delete_limit(orphan_addresses, orphan_subnets, target,
                            section_ids, config)

    # Addresses first, then subnets deepest-first, so a parent is never
    # removed while a child still points at it.
    actions.extend(orphan_addresses)
    actions.extend(sorted(orphan_subnets,
                          key=lambda a: -a.detail.get("prefixlen", 0)))
    return actions


def _prefixlen(cidr):
    try:
        return ipaddress.ip_network(cidr, strict=False).prefixlen
    except ValueError:
        return 0


def _check_delete_limit(orphan_addresses, orphan_subnets, target,
                        section_ids, config):
    """Refuses the whole run when it would delete an implausible share of
    the target.

    The failure this exists for is not one wrongly-deleted record. It is
    an empty or mis-scoped snapshot arriving over a one-way link the
    replica cannot question, and taking the entire replica with it. A run
    that wants to remove most of your IPAM should stop and ask a human.
    """
    deletions = len(orphan_addresses) + len(orphan_subnets)
    if not deletions:
        return

    options = config["options"]
    if options.get("force_delete"):
        return

    in_scope = 0
    for section_id in section_ids.values():
        if section_id is None:
            continue
        subnets = target.subnets_by_cidr(section_id)
        in_scope += len(subnets)
        for subnet in subnets.values():
            in_scope += len(target.addresses_by_ip(subnet["id"]))

    fraction = options.get("delete_limit_fraction", 0.1)
    allowed = max(DELETE_LIMIT_FLOOR, int(in_scope * fraction))
    if deletions <= allowed:
        return

    raise PlanError(
        f"Refusing to run: this would delete {deletions} of {in_scope} "
        f"in-scope record(s) on the target ({deletions / in_scope:.0%}), "
        f"which is over the safety limit of {allowed} "
        f"({fraction:.0%}, floor {DELETE_LIMIT_FLOOR}).\n"
        f"This usually means the snapshot is empty, truncated, or was "
        f"exported from the wrong section -- NOT that this much really "
        f"was deleted upstream. Check the source instance and the "
        f"snapshot's manifest.json first.\n"
        f"If the deletions are genuinely correct, re-run with "
        f"--force-delete, or raise options.delete_limit_fraction."
    )


def summarise(actions):
    """Counts actions by kind, for the run summary line."""
    counts = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    return counts


def canonicalise_document_cidr(document):
    """Normalises a document's CIDR through the stdlib, so a snapshot
    written by an older exporter (or hand-edited) still matches target
    records on the same natural key."""
    network = ipaddress.ip_network(document["cidr"], strict=False)
    document["cidr"] = str(network)
    if document.get("master_subnet"):
        document["master_subnet"] = canonical_cidr(
            *document["master_subnet"].split("/")
        )
    return document
