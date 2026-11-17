"""
ipamsync.plan

Turns "snapshot + current target state" into an ordered list of actions,
without performing any of them. A dry run prints the same plan `--apply`
executes.

## Additive by default

Records on the target but not in the snapshot become `drift_*` actions:
reported, never acted on.

## Deleting

`options.delete_drift` (or `--delete`) turns them into `delete_*` actions
(strict mirror). Guards:

  1. Still requires `--apply`.
  2. Scoped to the configured sections and the subnets/addresses in them.
  3. Safety limit: the run is refused if it would delete more than
     `options.delete_limit_fraction` (default 0.1) of in-scope target
     records, e.g. because the snapshot is empty or mis-scoped.
     `--force-delete` overrides it for one run.

Deletion order: addresses, then subnets deepest-first, then folders.

## Re-parenting

`masterSubnetId` is a local id, so parents are compared as CIDRs and a
change emits `reparent_subnet`. phpIPAM refuses to create a subnet that
overlaps its siblings (`409 Subnet overlaps with ...`), so a new
10.20.4.0/22 cannot be created under 10.20.0.0/16 while 10.20.5.0/24 is
still a sibling there. Each re-parent is therefore a `detach_subnet` (move
to top level; PATCH is not overlap-checked) plus a `reparent_subnet`, and
all detaches run before any create. A new aggregate above subnets that are
already top-level cannot be created and is reported.

## Matching is by natural key

Subnets match on (target section, canonical CIDR); addresses on (subnet,
IP). Ids are instance-local and never carried.
"""

import ipaddress

from ipamsync import model
from ipamsync.snapshot import canonical_cidr, partition_documents

# Action kinds that change the target.
WRITE_KINDS = (
    "create_section",
    "create_subnet",
    "update_subnet",
    # Paired with, and before, a reparent (see "Re-parenting").
    "detach_subnet",
    "reparent_subnet",
    # A subnet may live in a folder; folders nest only under folders.
    "create_folder",
    # Planned before the subnets that reference them.
    "create_l2domain",
    "update_l2domain",
    "attach_l2domain",
    "create_vlan",
    "update_vlan",
    "create_vrf",
    "update_vrf",
    "link_subnet",
    "create_address",
    "update_address",
    # Only with deletion enabled (see "Deleting").
    "delete_address",
    "delete_subnet",
    "delete_folder",
    "delete_vlan",
    "delete_vrf",
)
# Action kinds that are reported only and never executed.
REPORT_KINDS = (
    "drift_subnet", "drift_address", "drift_folder", "drift_vlan",
    "drift_vrf", "note",
)

#: Deletions always allowed regardless of the fraction limit, so small
#: datasets are not blocked.
DELETE_LIMIT_FLOOR = 5

#: Reason attached to an orphan, keyed by whether deletion is enabled.
ORPHAN_REASON = {
    False: ("exists on target, absent from snapshot "
            "(not deleted -- deletion is disabled)"),
    True: "absent from snapshot -- deleting (strict mirror)",
}


class PlanError(Exception):
    """Raised when a plan cannot be built (e.g. a missing target section
    that may not be created)."""


class Action:
    """One planned change, keyed by natural key (CIDR, or CIDR + IP).
    `detail` holds the fields to write, or a report-only reason."""

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
        if self.kind == "link_subnet":
            bits = []
            if "vlan" in self.detail:
                vlan = self.detail["vlan"]
                bits.append("vlan=" + (
                    f"{vlan['domain']}/{vlan['number']}" if vlan else "none"))
            if "vrf" in self.detail:
                vrf = self.detail["vrf"]
                bits.append("vrf=" + (vrf["name"] if vrf else "none"))
            return f"{self.kind:16} {self.key}  [{', '.join(bits)}]"
        if self.kind == "reparent_subnet":
            was = self.detail.get("was") or "top level"
            folder = self.detail.get("parent_folder")
            now = ("/".join(folder) if folder
                   else self.detail.get("master_subnet") or "top level")
            return f"{self.kind:16} {self.key}  [{was} -> {now}]"
        if self.kind in ("drift_subnet", "drift_address", "drift_folder",
                         "drift_vlan", "drift_vrf", "note"):
            return f"{self.kind:16} {self.key}  -- {self.detail.get('reason', '')}"
        return f"{self.kind:16} {self.key}"


def _fold(value):
    """Case-insensitive comparison key; "" and None are equal."""
    return str(value or "").strip().casefold()


def _vlan_key(reference):
    """Comparable VLAN reference: (folded domain name, number string)."""
    if not reference:
        return None
    return (_fold(reference.get("domain")), str(reference.get("number") or ""))


def _current_parent_cidr(current, cidr_by_id):
    """CIDR of a target subnet's current parent, or None for top level.
    "No parent" is 0 or "0"; an unresolvable parent id also reads as top
    level."""
    raw = current.get("masterSubnetId")
    if raw in (None, "", 0, "0"):
        return None
    return cidr_by_id.get(str(raw))


def order_documents(documents):
    """Orders subnet documents so a parent always precedes its children.
    Parents outside the snapshot are ignored; cycles are broken."""
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
    """'10.20.0.0/24' -> ('10.20.0.0', '24'), as phpIPAM's create wants."""
    network = ipaddress.ip_network(cidr, strict=False)
    return str(network.network_address), str(network.prefixlen)


def build_plan(documents, target, config):
    """Builds the ordered action list. `target` is a TargetView (see
    ipamsync.target) or a fake with the same methods."""
    from ipamsync.config import target_section_name

    options = config["options"]
    deleting = bool(options.get("delete_drift"))
    actions = []
    # Collected separately for the safety limit and deletion ordering.
    orphan_addresses = []
    orphan_subnets = []
    # Orphan folders, VLANs and VRFs; ordered after orphan subnets.
    orphan_l2 = []
    orphan_folders = []
    detaches = []

    documents, section_docs = partition_documents(documents)
    documents = order_documents(documents)
    sections_needed = sorted(
        {document["section"] for document in documents}
        | {document["section"] for document in section_docs}
    )

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

    # -- L2 domains, VLANs and VRFs (before any subnet) ----------------
    for document in sorted(section_docs, key=lambda d: d["section"]):
        source_section = document["section"]
        section_id = section_ids[source_section]

        # Folders, shallowest first (re-sorted for hand-written snapshots).
        existing_folders = (
            target.folders_by_path(section_id) if section_id is not None else {}
        )
        for path in sorted((tuple(p) for p in document.get("folders") or []),
                           key=lambda p: (len(p), p)):
            if path in existing_folders:
                continue
            actions.append(Action(
                "create_folder", "/".join(path), {"path": list(path)},
                section=source_section,
            ))

        for domain in document.get("vlan_domains") or []:
            name = domain.get("name")
            if not name:
                continue
            existing = target.domain_by_name(name)
            if existing is None:
                actions.append(Action(
                    "create_l2domain", name, dict(domain),
                    section=source_section,
                ))
                continue
            changed = model.diff_fields(domain, existing)
            if changed:
                actions.append(Action(
                    "update_l2domain", name, changed, section=source_section,
                ))
            # Add this section to the domain, never overwrite: on a fan-in
            # master a domain may serve several sources.
            if section_id is not None:
                listed = {
                    part.strip()
                    for part in str(existing.get("sections") or "").split(";")
                    if part.strip()
                }
                if str(section_id) not in listed and str(existing.get("id")) != "1":
                    actions.append(Action(
                        "attach_l2domain", name,
                        {"section_id": section_id}, section=source_section,
                    ))

        for vlan in document.get("vlans") or []:
            domain_name, number = vlan.get("domain"), str(vlan.get("number") or "")
            if not number:
                continue
            key = f"{domain_name}/{number}"
            existing = target.vlan_by_key(domain_name, number)
            if existing is None:
                actions.append(Action(
                    "create_vlan", key, dict(vlan), section=source_section,
                ))
                continue
            wanted = {k: v for k, v in vlan.items() if k != "domain"}
            changed = model.diff_fields(wanted, existing)
            if changed:
                changed = dict(changed)
                changed["domain"] = domain_name
                changed["number"] = number
                actions.append(Action(
                    "update_vlan", key, changed, section=source_section,
                ))

        # -- Orphans: on the target, absent from this snapshot ---------
        #
        # Only VLANs/VRFs scoped to this section alone are candidates: a
        # domain can serve several sections, and domain 1 serves all.
        if section_id is not None:
            snapshot_folders = {
                tuple(path) for path in document.get("folders") or []
            }
            for path in sorted(existing_folders, key=lambda p: (-len(p), p)):
                if path in snapshot_folders:
                    continue
                orphan_folders.append(Action(
                    "delete_folder" if deleting else "drift_folder",
                    "/".join(path),
                    {"path": list(path), "reason": ORPHAN_REASON[deleting],
                     "depth": len(path)},
                    section=source_section,
                ))

            snapshot_vlans = {
                (str(v.get("domain") or "").strip().casefold(),
                 str(v.get("number") or ""))
                for v in document.get("vlans") or []
            }
            for domain in target.domains_owned_by(section_id):
                for raw in target.vlans_in_domain(domain.get("name")):
                    key = (str(domain.get("name") or "").strip().casefold(),
                           str(raw.get("number") or ""))
                    if key in snapshot_vlans:
                        continue
                    orphan_l2.append(Action(
                        "delete_vlan" if deleting else "drift_vlan",
                        f"{domain.get('name')}/{raw.get('number')}",
                        {"domain": domain.get("name"),
                         "number": str(raw.get("number") or ""),
                         "reason": ORPHAN_REASON[deleting]},
                        section=source_section,
                    ))

            snapshot_vrfs = {
                str(v.get("name") or "").strip().casefold()
                for v in document.get("vrfs") or []
            }
            for raw in target.vrfs_owned_by(section_id):
                if str(raw.get("name") or "").strip().casefold() in snapshot_vrfs:
                    continue
                orphan_l2.append(Action(
                    "delete_vrf" if deleting else "drift_vrf",
                    raw.get("name") or "",
                    {"reason": ORPHAN_REASON[deleting]},
                    section=source_section,
                ))

        for vrf in document.get("vrfs") or []:
            name = vrf.get("name")
            if not name:
                continue
            existing = (
                target.vrf_by_name(name, section_id)
                if section_id is not None else None
            )
            if existing is None:
                actions.append(Action(
                    "create_vrf", name, dict(vrf), section=source_section,
                ))
                continue
            changed = model.diff_fields(vrf, existing)
            if changed:
                actions.append(Action(
                    "update_vrf", name, changed, section=source_section,
                ))

    # -- Subnets and their addresses -----------------------------------
    snapshot_cidrs_by_section = {}
    cidr_by_id_by_section = {}
    for document in documents:
        source_section = document["section"]
        cidr = document["cidr"]
        snapshot_cidrs_by_section.setdefault(source_section, set()).add(cidr)

        section_id = section_ids[source_section]
        existing_subnets = (
            target.subnets_by_cidr(section_id) if section_id is not None else {}
        )
        # id -> CIDR, for reading a current parent back as a CIDR.
        if source_section not in cidr_by_id_by_section:
            cidr_by_id_by_section[source_section] = {
                str(record.get("id")): existing_cidr
                for existing_cidr, record in existing_subnets.items()
            }
        cidr_by_id = cidr_by_id_by_section[source_section]
        current = existing_subnets.get(cidr)

        if current is None:
            actions.append(Action(
                "create_subnet", cidr,
                {
                    "fields": document["fields"],
                    "master_subnet": document.get("master_subnet"),
                    "parent_folder": document.get("parent_folder"),
                },
                section=source_section, cidr=cidr,
            ))
            # vlanId/vrfId are local ids, linked by natural key after create.
            wanted = {}
            if document.get("vlan"):
                wanted["vlan"] = document["vlan"]
            if document.get("vrf"):
                wanted["vrf"] = document["vrf"]
            if wanted:
                actions.append(Action(
                    "link_subnet", cidr, wanted,
                    section=source_section, cidr=cidr,
                ))
        else:
            changed = model.diff_fields(document["fields"], current)
            if changed:
                actions.append(Action(
                    "update_subnet", cidr, changed,
                    section=source_section, cidr=cidr,
                ))

            # VLAN/VRF are local ids, so compared here by natural key.
            wanted = {}
            desired_vlan = document.get("vlan")
            current_vlan = target.vlan_ref_for_id(current.get("vlanId"))
            if _vlan_key(desired_vlan) != _vlan_key(current_vlan):
                wanted["vlan"] = desired_vlan
            desired_vrf = (document.get("vrf") or {}).get("name")
            current_vrf = target.vrf_name_for_id(current.get("vrfId"))
            if _fold(desired_vrf) != _fold(current_vrf):
                wanted["vrf"] = document.get("vrf")
            if wanted:
                actions.append(Action(
                    "link_subnet", cidr, wanted,
                    section=source_section, cidr=cidr,
                ))

            # Parent compared by natural key: a CIDR, or a folder path
            # (phpIPAM stores both in masterSubnetId).
            wanted_folder = document.get("parent_folder")
            wanted_folder = tuple(wanted_folder) if wanted_folder else None
            current_folder = (
                target.folder_path_for_id(section_id, current.get("masterSubnetId"))
                if section_id is not None else None
            )
            wanted_parent = document.get("master_subnet")
            current_parent = (
                None if current_folder
                else _current_parent_cidr(current, cidr_by_id)
            )
            if wanted_folder or current_folder:
                if wanted_folder != current_folder:
                    actions.append(Action(
                        "reparent_subnet", cidr,
                        {"parent_folder": list(wanted_folder) if wanted_folder
                         else None,
                         "master_subnet": wanted_parent,
                         "was": "/".join(current_folder) if current_folder
                         else current_parent},
                        section=source_section, cidr=cidr,
                    ))
            elif wanted_parent != current_parent:
                # Only when the new parent exists or is created earlier in
                # this run.
                resolvable = (
                    wanted_parent is None
                    or wanted_parent in existing_subnets
                    or wanted_parent in snapshot_cidrs_by_section[source_section]
                )
                if resolvable:
                    if current_parent is not None:
                        # Detach before any create (sibling overlap check;
                        # see "Re-parenting").
                        detaches.append(Action(
                            "detach_subnet", cidr,
                            {"was": current_parent},
                            section=source_section, cidr=cidr,
                        ))
                    actions.append(Action(
                        "reparent_subnet", cidr,
                        {"master_subnet": wanted_parent, "was": current_parent},
                        section=source_section, cidr=cidr,
                    ))
                else:
                    actions.append(Action(
                        "note", f"{cidr} parent {wanted_parent}",
                        {"reason": f"moved under {wanted_parent} on the source, "
                                   f"which is not replicated -- left where it is"},
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
            # Addresses in a deleted subnet go with it; do not count them.
            doomed = {(a.section, a.cidr) for a in orphan_subnets}
            orphan_addresses = [a for a in orphan_addresses
                                if (a.section, a.cidr) not in doomed]

    if deleting:
        _check_delete_limit(orphan_addresses,
                            orphan_subnets + orphan_folders + orphan_l2,
                            target, section_ids, config)

    # Section creates, then detaches, then everything else.
    section_creates = [a for a in actions if a.kind == "create_section"]
    remainder = [a for a in actions if a.kind != "create_section"]
    actions = section_creates + detaches + remainder

    # Deletion order: addresses, subnets deepest-first, folders
    # deepest-first (phpIPAM deletes a folder's contents with it), then
    # VLANs/VRFs (deleting one only nulls subnet references).
    actions.extend(orphan_addresses)
    actions.extend(sorted(orphan_subnets,
                          key=lambda a: -a.detail.get("prefixlen", 0)))
    actions.extend(sorted(orphan_folders,
                          key=lambda a: -a.detail.get("depth", 0)))
    actions.extend(orphan_l2)
    return actions


def _prefixlen(cidr):
    try:
        return ipaddress.ip_network(cidr, strict=False).prefixlen
    except ValueError:
        return 0


def _check_delete_limit(orphan_addresses, orphan_subnets, target,
                        section_ids, config):
    """Raises PlanError if the run would delete more than the allowed
    share of in-scope target records (guards against a bad snapshot)."""
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
    """Canonicalises a subnet document's CIDR and master_subnet, so older
    or hand-edited snapshots still match. Section documents pass through."""
    if document.get("kind") == "section" or "cidr" not in document:
        return document
    network = ipaddress.ip_network(document["cidr"], strict=False)
    document["cidr"] = str(network)
    if document.get("master_subnet"):
        document["master_subnet"] = canonical_cidr(
            *document["master_subnet"].split("/")
        )
    return document
