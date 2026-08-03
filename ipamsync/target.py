"""
ipamsync.target

Reading the target instance's current state, and executing a plan
against it.

## Reads are cached, deliberately

A plan is built by asking "does this subnet exist? do these addresses
exist?" for every record in the snapshot. Asking phpIPAM that
per-record would be one HTTP request per address on a subnet that might
hold hundreds. TargetView instead fetches each section's subnets once
and each subnet's addresses once, and indexes them by natural key. A
sync of a few thousand addresses costs a handful of requests.

The cache is invalidated narrowly after a create, so a subnet created
early in a run is visible to the addresses planned into it later.

## Writes are confirmed, not assumed

Every write method in the vendored client is documented as unverified
against a live phpIPAM instance -- the response shape of a POST comes
from the API docs, not from observation. `resolve_created()` therefore
never trusts the id a create returns: it re-reads the record by natural
key and uses the id it finds. If a create silently no-ops, the re-read
fails and the run stops with a clear error, instead of the next 200
addresses being written against a bogus subnet id.
"""

import re

from ipamsync import model
from ipamsync.model import CUSTOM_FIELD_PREFIX
from phpipam_client import PhpIpamError
from ipamsync.plan import split_cidr
from ipamsync.snapshot import canonical_cidr


class TargetError(Exception):
    """Raised when the target instance is in a state the importer cannot
    safely proceed from."""


class TargetView:
    """Cached, natural-key-indexed view of the target instance."""

    def __init__(self, client):
        self._client = client
        self._sections = None
        self._subnets = {}     # section_id -> {cidr: raw subnet}
        self._addresses = {}   # subnet_id  -> {ip: raw address}
        self._l2 = None        # domains/vlans/vrfs, indexed by natural key
        self._folders = {}     # section_id -> ({path: raw}, {id: path})

    # -- Sections ------------------------------------------------------

    def _load_sections(self):
        if self._sections is None:
            self._sections = self._client.get_sections()
        return self._sections

    def section_by_name(self, name):
        """Case-insensitive match -- phpIPAM section names are free text
        and 'Shared' vs 'shared' across two instances is a configuration
        slip, not a reason to create a duplicate section."""
        wanted = str(name).strip().casefold()
        for section in self._load_sections():
            if str(section.get("name", "")).strip().casefold() == wanted:
                return section
        return None

    def invalidate_sections(self):
        self._sections = None

    # -- Subnets -------------------------------------------------------

    def subnets_by_cidr(self, section_id):
        """All subnets in a section, indexed by canonical CIDR.

        Includes nested subnets: phpIPAM's sections/{id}/subnets/ returns
        every subnet in the section regardless of nesting depth, which is
        what we want -- a child subnet is matched by its own CIDR, not by
        where it sits in the tree.
        """
        if section_id not in self._subnets:
            index = {}
            for raw in self._client.get_subnets_in_section(section_id):
                if not raw.get("subnet") or raw.get("mask") in (None, ""):
                    continue  # folders and malformed rows have no network
                try:
                    key = canonical_cidr(raw["subnet"], raw["mask"])
                except ValueError:
                    continue
                # Flattened so custom fields compare against the
                # snapshot, which stores them flat -- see
                # model.flatten_custom_fields.
                index[key] = model.flatten_custom_fields(raw)
            self._subnets[section_id] = index
        return self._subnets[section_id]

    def invalidate_subnets(self, section_id):
        self._subnets.pop(section_id, None)
        self._folders.pop(section_id, None)

    # -- Folders -------------------------------------------------------
    #
    # A phpIPAM folder is a `subnets` row with isFolder=1: no network,
    # and its name in `description`. Indexed by path -- a tuple of names
    # from the section root -- because names are not unique across
    # different parents. Folders may only nest under folders (the API
    # answers `409 Parent is not a folder` otherwise), so walking
    # masterSubnetId upwards always terminates.

    def folders_by_path(self, section_id):
        if section_id not in self._folders:
            raw_folders = {
                str(raw["id"]): raw
                for raw in self._client.get_subnets_in_section(section_id)
                if str(raw.get("isFolder") or "0") == "1"
            }
            index, path_by_id = {}, {}

            def resolve(folder_id, seen):
                if folder_id in path_by_id:
                    return path_by_id[folder_id]
                raw = raw_folders.get(folder_id)
                if raw is None or folder_id in seen:
                    return None
                seen.add(folder_id)
                name = str(raw.get("description") or "").strip()
                parent_id = str(raw.get("masterSubnetId") or "0")
                if parent_id in ("0", "", "None") or parent_id not in raw_folders:
                    path = (name,)
                else:
                    parent = resolve(parent_id, seen)
                    path = ((*parent, name) if parent else (name,))
                path_by_id[folder_id] = path
                return path

            for folder_id, raw in raw_folders.items():
                path = resolve(folder_id, set())
                if path:
                    index[path] = raw
            self._folders[section_id] = (index, path_by_id)
        return self._folders[section_id][0]

    def folder_path_for_id(self, section_id, folder_id):
        """The path a subnet's masterSubnetId points at, when it points
        at a folder, or None when it points at a subnet or nothing."""
        if str(folder_id or "") in ("", "0", "None"):
            return None
        self.folders_by_path(section_id)
        return self._folders[section_id][1].get(str(folder_id))

    # -- L2 domains, VLANs and VRFs ------------------------------------
    #
    # Indexed by natural key like everything else: a domain by name, a
    # VLAN by (domain name, number), a VRF by (section id, name). The
    # reverse maps exist so a subnet's current vlanId/vrfId can be read
    # back as a natural key and compared against the snapshot -- the same
    # move that makes re-parenting work, and for the same reason: an id
    # on its own says nothing that can be compared across instances.

    def _load_l2(self):
        if self._l2 is None:
            domains = self._client.get_l2domains()
            domain_name_by_id = {
                str(d.get("id")): (d.get("name") or "") for d in domains
            }
            vlans, vlan_ref_by_id = {}, {}
            for raw in self._client.get_vlans():
                domain = domain_name_by_id.get(str(raw.get("domainId") or "1"), "")
                key = (domain.strip().casefold(), str(raw.get("number") or ""))
                vlans[key] = raw
                vlan_ref_by_id[str(raw.get("id"))] = {
                    "domain": domain, "number": str(raw.get("number") or ""),
                }
            vrfs, vrf_name_by_id = [], {}
            for raw in self._client.get_vrfs():
                vrfs.append(raw)
                vrf_name_by_id[str(raw.get("id"))] = raw.get("name") or ""
            self._l2 = {
                "domains": {
                    str(d.get("name") or "").strip().casefold(): d
                    for d in domains
                },
                "vlans": vlans,
                "vlan_ref_by_id": vlan_ref_by_id,
                "vrfs": vrfs,
                "vrf_name_by_id": vrf_name_by_id,
            }
        return self._l2

    def domain_by_name(self, name):
        return self._load_l2()["domains"].get(str(name).strip().casefold())

    def vlan_by_key(self, domain_name, number):
        return self._load_l2()["vlans"].get(
            (str(domain_name).strip().casefold(), str(number))
        )

    def vrf_by_name(self, name, section_id):
        """VRFs are keyed by (section, name), never by name alone.

        phpIPAM puts no unique index on vrf.name, so the same name in two
        sections is two separate records -- which is what keeps fan-in
        safe. Matching on name alone would make every subordinate adopt
        and overwrite the first site's VRF of that name.
        """
        wanted = str(name).strip().casefold()
        section_id = str(section_id)
        for raw in self._load_l2()["vrfs"]:
            if str(raw.get("name") or "").strip().casefold() != wanted:
                continue
            listed = {
                part.strip()
                for part in str(raw.get("sections") or "").split(";")
                if part.strip()
            }
            if section_id in listed:
                return raw
        return None

    def vlan_ref_for_id(self, vlan_id):
        """The (domain, number) a subnet's vlanId currently points at, or
        None where it points at nothing."""
        if str(vlan_id or "") in ("", "0", "None"):
            return None
        return self._load_l2()["vlan_ref_by_id"].get(str(vlan_id))

    def vrf_name_for_id(self, vrf_id):
        if str(vrf_id or "") in ("", "0", "None"):
            return None
        return self._load_l2()["vrf_name_by_id"].get(str(vrf_id))

    def _section_list(self, raw, key="sections"):
        return {
            part.strip()
            for part in str(raw.get(key) or "").split(";")
            if part.strip()
        }

    def domains_owned_by(self, section_id):
        """L2 domains scoped to this section and NO other.

        Deletion candidates have to be limited to these. Domain 1
        ("default") belongs to every section implicitly and its section
        list is empty, and any domain serving several sections is shared
        on a fan-in master -- deleting VLANs out of either would let one
        subordinate remove another's, which is exactly what the
        one-section-per-source rule exists to prevent.
        """
        owned = []
        for raw in self._load_l2()["domains"].values():
            if str(raw.get("id")) == "1":
                continue
            if self._section_list(raw) == {str(section_id)}:
                owned.append(raw)
        return owned

    def vlans_in_domain(self, domain_name):
        wanted = str(domain_name).strip().casefold()
        return [raw for (domain, _number), raw
                in self._load_l2()["vlans"].items() if domain == wanted]

    def vrfs_owned_by(self, section_id):
        """VRFs scoped to this section and no other -- same reasoning as
        domains_owned_by. A VRF shared with another section is left
        alone; removing it would take it away from that section too."""
        return [raw for raw in self._load_l2()["vrfs"]
                if self._section_list(raw) == {str(section_id)}]

    def subnets_under(self, section_id, parent_id):
        """Rows -- subnets or folders -- whose parent is this one.

        Used before deleting a folder. phpIPAM deletes a folder's entire
        contents with it, silently, so emptiness is confirmed first.
        """
        return [
            raw for raw in self._client.get_subnets_in_section(section_id)
            if str(raw.get("masterSubnetId") or "0") == str(parent_id)
        ]

    def invalidate_l2(self):
        self._l2 = None

    # -- Addresses -----------------------------------------------------

    def addresses_by_ip(self, subnet_id):
        if subnet_id not in self._addresses:
            index = {}
            for raw in self._client.get_addresses_in_subnet(subnet_id):
                ip = raw.get("ip")
                if ip:
                    index[str(ip)] = model.flatten_custom_fields(raw)
            self._addresses[subnet_id] = index
        return self._addresses[subnet_id]

    def invalidate_addresses(self, subnet_id):
        self._addresses.pop(subnet_id, None)


def _split_fields(fields):
    """Separates custom fields from stock ones. Both go on the wire the
    same way; they are split only so a rejected custom field can be named
    precisely in the error message (the overwhelmingly common cause of a
    write failing on an otherwise-healthy target is a custom field that
    exists on the source instance but was never created on the target)."""
    stock, custom = {}, {}
    for key, value in fields.items():
        (custom if key.startswith(CUSTOM_FIELD_PREFIX) else stock)[key] = value
    return stock, custom


class Executor:
    """Runs a plan's write actions against the target instance.

    Failures are per-record: one address that phpIPAM rejects is
    recorded and the run continues. A one-way sync that aborts on the
    first bad record would need a human to intervene before *any* of the
    remaining good records landed, and the next run would hit the same
    record and stop again. Errors are collected and reported at the end,
    and the exit status reflects them.
    """

    def __init__(self, client, view, config, logger):
        self._client = client
        self._view = view
        self._config = config
        self._log = logger
        self._section_ids = {}   # source section name -> target section id
        self._subnet_ids = {}    # (section id, cidr)   -> target subnet id
        self.errors = []
        self.applied = 0

    # -- id resolution --------------------------------------------------

    def _section_id(self, source_section):
        from ipamsync.config import target_section_name

        if source_section in self._section_ids:
            return self._section_ids[source_section]
        wanted = target_section_name(self._config, source_section)
        section = self._view.section_by_name(wanted)
        if not section:
            raise TargetError(
                f"Section {wanted!r} not found on the target while applying. "
                f"It existed at plan time or was created earlier in this run; "
                f"something else changed the target concurrently."
            )
        self._section_ids[source_section] = section["id"]
        return section["id"]

    def _subnet_id(self, source_section, cidr):
        section_id = self._section_id(source_section)
        cached = self._subnet_ids.get((section_id, cidr))
        if cached is not None:
            return cached
        subnet = self._view.subnets_by_cidr(section_id).get(cidr)
        if not subnet:
            raise TargetError(
                f"Subnet {cidr} not found in target section id {section_id} "
                f"while applying. Expected it to exist or to have been "
                f"created earlier in this run."
            )
        self._subnet_ids[(section_id, cidr)] = subnet["id"]
        return subnet["id"]

    # -- actions --------------------------------------------------------

    def apply(self, actions):
        for action in actions:
            if not action.is_write:
                continue
            try:
                handler = getattr(self, f"_do_{action.kind}")
                handler(action)
                self.applied += 1
            except Exception as exc:  # noqa: BLE001 -- per-record isolation
                self._record_error(action, exc)
        return self.applied, self.errors

    #: phpIPAM's own wording when a write names a column it does not have.
    _INVALID_KEY = re.compile(r"Invalid request key (\S+)")

    def _record_error(self, action, exc):
        message = f"{action.kind} {action.key}: {exc}"

        # phpIPAM names the offending column, so quote THAT rather than
        # guessing from the payload. The previous version listed whatever
        # `custom_*` fields the record happened to carry, which pointed at
        # the wrong field entirely whenever the real culprit was a custom
        # field without that prefix -- exactly the common case, since
        # phpIPAM does not require the prefix.
        rejected = self._INVALID_KEY.search(str(exc))
        if rejected:
            field = rejected.group(1)
            message += (
                f"  ({field!r} exists on the source instance but not on this "
                f"one. If it is a custom field, create it here too under "
                f"Administration > Custom fields, with the same name. Custom "
                f"fields are NOT created automatically -- they are schema, "
                f"not data.)"
            )
        else:
            _, custom = _split_fields(action.detail.get("fields", action.detail))
            if custom and "custom" not in str(exc).lower():
                message += (
                    f"  (this record carries custom field(s) "
                    f"{sorted(custom)} -- confirm they exist on the target "
                    f"instance under Administration > Custom fields)"
                )
        self.errors.append(message)
        self._log(f"  ERROR  {message}")

    def _do_create_section(self, action):
        self._client.create_section(action.detail["name"])
        # Do not trust the returned id -- confirm by re-reading.
        self._view.invalidate_sections()
        if not self._view.section_by_name(action.detail["name"]):
            raise TargetError(
                f"created section {action.detail['name']!r} but it is not "
                f"visible on re-read -- treating as a failed write"
            )

    def _do_create_subnet(self, action):
        section_id = self._section_id(action.section)
        network, prefix = split_cidr(action.cidr)
        extra = model.to_subnet_write_fields(action.detail["fields"])

        parent_folder = action.detail.get("parent_folder")
        parent_cidr = action.detail.get("master_subnet")
        if parent_folder:
            folder = self._view.folders_by_path(section_id).get(
                tuple(parent_folder))
            if folder:
                extra["masterSubnetId"] = folder["id"]
            else:
                self._log(
                    f"  NOTE   {action.cidr} lives in folder "
                    f"{'/'.join(parent_folder)} on the source, which is not "
                    f"present on the target -- creating it top-level instead"
                )
        elif parent_cidr:
            parent = self._view.subnets_by_cidr(section_id).get(parent_cidr)
            if parent:
                extra["masterSubnetId"] = parent["id"]
            else:
                self._log(
                    f"  NOTE   {action.cidr} nests under {parent_cidr} on the "
                    f"source, which is not present on the target -- creating "
                    f"it top-level in the section instead"
                )

        try:
            self._client.create_subnet(
                subnet=network, mask=prefix, section_id=section_id, **extra
            )
        except PhpIpamError as exc:
            # phpIPAM validates a new subnet against its siblings, and
            # refuses one that overlaps them. The plan already detaches
            # subnets it is about to re-nest, so reaching this means the
            # overlapping subnet is staying where it is -- most often a
            # new aggregate being added *above* existing top-level
            # subnets, which phpIPAM will not accept while they are its
            # siblings. Nothing this tool can reorder fixes that, so say
            # so rather than surfacing a bare 409.
            if "overlap" in str(exc).lower() or "nested subnet" in str(exc).lower():
                raise TargetError(
                    f"{exc}. phpIPAM refuses a subnet that overlaps one of "
                    f"its siblings. {action.cidr} is new upstream and sits "
                    f"above a subnet that already exists here at the same "
                    f"level, so there is nowhere to move that one to first. "
                    f"The fix is on the target: turn OFF 'strict mode' on "
                    f"this section (Administration > Sections > edit), which "
                    f"disables the overlap and nesting checks for it. That is "
                    f"safe for a replicated section -- the hierarchy written "
                    f"here is one the source instance already validated. "
                    f"Otherwise, nest or remove the overlapping subnet by "
                    f"hand; this tool does not touch the addresses in it."
                )
            raise
        # Confirm by natural key rather than trusting the POST's return.
        self._view.invalidate_subnets(section_id)
        if action.cidr not in self._view.subnets_by_cidr(section_id):
            raise TargetError(
                f"created subnet {action.cidr} but it is not visible on "
                f"re-read of section id {section_id} -- treating as a failed "
                f"write (nothing further will be written into this subnet)"
            )

    def _do_update_subnet(self, action):
        subnet_id = self._subnet_id(action.section, action.cidr)
        self._client.update_subnet(
            subnet_id, **model.to_subnet_write_fields(action.detail)
        )

    def _do_delete_folder(self, action):
        """Deletes an orphaned folder, but only once it is empty.

        phpIPAM deletes a folder's entire contents along with it -- child
        folders, the subnets inside them and all their addresses --
        reporting only "Subnet deleted". Confirmed on 1.8.1. Nothing in
        the plan would show that blast radius, and the safety limit would
        count it as one deletion, so emptiness is re-checked here against
        the live instance rather than inferred from the plan.

        A folder that still has contents is left for a later run: by then
        its children have either been deleted in their own right or moved
        out, and both are visible as their own actions.
        """
        section_id = self._section_id(action.section)
        path = tuple(action.detail["path"])
        folder = self._view.folders_by_path(section_id).get(path)
        if not folder:
            return  # already gone

        remaining = self._view.subnets_under(section_id, folder["id"])
        if remaining:
            self._log(
                f"  NOTE   folder {'/'.join(path)} is no longer in the "
                f"snapshot but still holds {len(remaining)} record(s) on the "
                f"target -- not deleting it, because phpIPAM would delete "
                f"those too. They will be removed in their own right first."
            )
            return

        self._client.delete_subnet(folder["id"])
        self._view.invalidate_subnets(section_id)

    def _do_delete_vlan(self, action):
        vlan = self._view.vlan_by_key(action.detail["domain"],
                                      action.detail["number"])
        if not vlan:
            return
        # Safe to do while subnets still point at it: phpIPAM nulls their
        # vlanId rather than deleting them. Confirmed on 1.8.1.
        self._client.delete_vlan(vlan["id"])
        self._view.invalidate_l2()

    def _do_delete_vrf(self, action):
        vrf = self._view.vrf_by_name(action.key, self._section_id(action.section))
        if not vrf:
            return
        self._client.delete_vrf(vrf["id"])
        self._view.invalidate_l2()

    def _do_create_folder(self, action):
        """Creates one folder, under its parent folder if it has one.

        Written through the subnets endpoint with isFolder=1, which is
        what phpIPAM's own Folders controller is -- it is `class
        Folders_controller extends Subnets_controller {}` and nothing
        else. The name goes in `description`; posting isFolder=1 makes
        phpIPAM discard `subnet` and `mask` itself.
        """
        section_id = self._section_id(action.section)
        path = tuple(action.detail["path"])
        parent_id = 0
        if len(path) > 1:
            parent = self._view.folders_by_path(section_id).get(path[:-1])
            if not parent:
                raise TargetError(
                    f"cannot create folder {'/'.join(path)} -- its parent "
                    f"folder {'/'.join(path[:-1])} is not present on the "
                    f"target"
                )
            parent_id = parent["id"]

        self._client.create_subnet(
            subnet=None, mask=None, section_id=section_id,
            description=path[-1], isFolder="1", masterSubnetId=parent_id,
        )
        self._view.invalidate_subnets(section_id)
        if path not in self._view.folders_by_path(section_id):
            raise TargetError(
                f"created folder {'/'.join(path)} but it is not visible on "
                f"re-read -- treating as a failed write"
            )

    # -- L2 domains, VLANs and VRFs ------------------------------------

    def _do_create_l2domain(self, action):
        section_id = self._section_id(action.section)
        self._client.create_l2domain(
            action.detail["name"], sections=[section_id],
            **{k: v for k, v in action.detail.items() if k != "name"}
        )
        self._view.invalidate_l2()
        if not self._view.domain_by_name(action.detail["name"]):
            raise TargetError(
                f"created L2 domain {action.detail['name']!r} but it is not "
                f"visible on re-read -- treating as a failed write"
            )

    def _do_update_l2domain(self, action):
        domain = self._view.domain_by_name(action.key)
        if not domain:
            raise TargetError(f"L2 domain {action.key!r} vanished before update")
        self._client.update_l2domain(domain["id"], **action.detail)
        self._view.invalidate_l2()

    def _do_attach_l2domain(self, action):
        """Adds the target section to an existing domain's section list.

        Additive on purpose: the domain may legitimately serve sections
        this source knows nothing about, and a fan-in master will have
        one domain per subordinate. Replacing the list would let each
        import evict the others.
        """
        domain = self._view.domain_by_name(action.key)
        if not domain:
            raise TargetError(f"L2 domain {action.key!r} vanished before update")
        section_id = str(self._section_id(action.section))
        listed = {
            part.strip()
            for part in str(domain.get("sections") or "").split(";")
            if part.strip()
        }
        listed.add(section_id)
        self._client.update_l2domain(domain["id"], sections=sorted(listed))
        self._view.invalidate_l2()

    def _do_create_vlan(self, action):
        domain_name = action.detail["domain"]
        domain = self._view.domain_by_name(domain_name)
        if not domain:
            raise TargetError(
                f"cannot create VLAN {action.detail.get('number')} -- its L2 "
                f"domain {domain_name!r} is not present on the target"
            )
        fields = {k: v for k, v in action.detail.items()
                  if k not in ("domain", "number", "name")}
        self._client.create_vlan(
            number=action.detail["number"],
            name=action.detail.get("name") or "",
            domain_id=domain["id"], **fields
        )
        self._view.invalidate_l2()
        if not self._view.vlan_by_key(domain_name, action.detail["number"]):
            raise TargetError(
                f"created VLAN {action.detail['number']} in domain "
                f"{domain_name!r} but it is not visible on re-read"
            )

    def _do_update_vlan(self, action):
        vlan = self._view.vlan_by_key(action.detail["domain"],
                                      action.detail["number"])
        if not vlan:
            raise TargetError(f"VLAN {action.key} vanished before update")
        fields = {k: v for k, v in action.detail.items() if k != "domain"}
        self._client.update_vlan(vlan["id"], **fields)
        self._view.invalidate_l2()

    def _do_create_vrf(self, action):
        section_id = self._section_id(action.section)
        fields = {k: v for k, v in action.detail.items() if k != "name"}
        self._client.create_vrf(
            name=action.detail["name"], sections=[section_id], **fields
        )
        self._view.invalidate_l2()
        if not self._view.vrf_by_name(action.detail["name"], section_id):
            raise TargetError(
                f"created VRF {action.detail['name']!r} but it is not visible "
                f"on re-read -- treating as a failed write"
            )

    def _do_update_vrf(self, action):
        vrf = self._view.vrf_by_name(action.key, self._section_id(action.section))
        if not vrf:
            raise TargetError(f"VRF {action.key!r} vanished before update")
        self._client.update_vrf(vrf["id"], **action.detail)
        self._view.invalidate_l2()

    def _do_link_subnet(self, action):
        """Points a subnet at its VLAN and/or VRF.

        Both are local ids, resolved here from the natural keys the
        snapshot carries. Sent in one PATCH because phpIPAM accepts them
        together and a subnet's VLAN and VRF change as a pair often
        enough to be worth not doing twice.
        """
        section_id = self._section_id(action.section)
        subnet_id = self._subnet_id(action.section, action.cidr)
        fields = {}

        if "vlan" in action.detail:
            reference = action.detail["vlan"]
            if reference is None:
                fields["vlanId"] = 0
            else:
                vlan = self._view.vlan_by_key(reference["domain"],
                                              reference["number"])
                if not vlan:
                    self._log(
                        f"  NOTE   {action.cidr} wants VLAN "
                        f"{reference['number']} in domain "
                        f"{reference['domain']!r}, which is not on the target "
                        f"-- leaving its VLAN as it is"
                    )
                else:
                    fields["vlanId"] = vlan["id"]

        if "vrf" in action.detail:
            reference = action.detail["vrf"]
            if reference is None:
                fields["vrfId"] = 0
            else:
                vrf = self._view.vrf_by_name(reference["name"], section_id)
                if not vrf:
                    self._log(
                        f"  NOTE   {action.cidr} wants VRF "
                        f"{reference['name']!r}, which is not on the target "
                        f"-- leaving its VRF as it is"
                    )
                else:
                    fields["vrfId"] = vrf["id"]

        if not fields:
            return
        self._client.update_subnet(subnet_id, **fields)
        self._view.invalidate_subnets(section_id)

    def _do_detach_subnet(self, action):
        """Moves a subnet temporarily to the top level of its section, so
        that a subnet it is about to be nested under can be created.

        phpIPAM validates overlap on create against the new subnet's
        siblings, so an existing /24 has to stop being a sibling before
        the /22 that will contain it can exist. The matching
        reparent_subnet immediately follows in the same plan. If a run
        dies in between, the subnet is left top-level and the next run
        plans the move again -- the comparison is against the snapshot,
        not against what this run intended.
        """
        section_id = self._section_id(action.section)
        subnet_id = self._subnet_id(action.section, action.cidr)
        self._client.update_subnet(subnet_id, masterSubnetId=0)
        self._view.invalidate_subnets(section_id)

    def _do_reparent_subnet(self, action):
        """Moves an existing subnet under a different parent, or out to
        the top level of its section.

        Kept separate from _do_update_subnet because the value written is
        a *local* id that has to be resolved here, at apply time, from
        the parent's CIDR -- the plan cannot know it, and the parent may
        itself have been created earlier in this same run.
        """
        section_id = self._section_id(action.section)
        subnet_id = self._subnet_id(action.section, action.cidr)

        parent_folder = action.detail.get("parent_folder")
        parent_cidr = action.detail.get("master_subnet")
        if parent_folder:
            folder = self._view.folders_by_path(section_id).get(
                tuple(parent_folder))
            if not folder:
                self._log(
                    f"  NOTE   {action.cidr} moved into folder "
                    f"{'/'.join(parent_folder)} on the source, which is not "
                    f"present on the target -- leaving it where it is"
                )
                return
            new_parent_id = folder["id"]
        elif parent_cidr:
            parent = self._view.subnets_by_cidr(section_id).get(parent_cidr)
            if not parent:
                self._log(
                    f"  NOTE   {action.cidr} moved under {parent_cidr} on the "
                    f"source, which is not present on the target -- leaving it "
                    f"where it is"
                )
                return
            new_parent_id = parent["id"]
        else:
            # phpIPAM spells "top level of the section" as 0, not null.
            new_parent_id = 0

        self._client.update_subnet(subnet_id, masterSubnetId=new_parent_id)
        self._view.invalidate_subnets(section_id)

    def _do_create_address(self, action):
        subnet_id = self._subnet_id(action.section, action.cidr)
        fields = dict(action.detail["fields"])
        hostname = fields.pop("hostname", None)
        description = fields.pop("description", None)
        self._client.create_address(
            subnet_id=subnet_id, ip=action.ip,
            hostname=hostname, description=description, **fields,
        )
        self._view.invalidate_addresses(subnet_id)

    def _do_delete_address(self, action):
        subnet_id = self._subnet_id(action.section, action.cidr)
        existing = self._view.addresses_by_ip(subnet_id).get(action.ip)
        if not existing:
            # Already gone -- nothing to do, and not worth failing over.
            return
        self._client.delete_address(existing["id"])
        self._view.invalidate_addresses(subnet_id)

    def _do_delete_subnet(self, action):
        section_id = self._section_id(action.section)
        subnet = self._view.subnets_by_cidr(section_id).get(action.cidr)
        if not subnet:
            return
        self._client.delete_subnet(subnet["id"])
        self._view.invalidate_subnets(section_id)
        self._view.invalidate_addresses(subnet["id"])
        self._subnet_ids.pop((section_id, action.cidr), None)
        # Confirm by re-read rather than trusting the DELETE's response,
        # for the same reason creates are confirmed: this endpoint has
        # never been exercised against a live instance by the vendored
        # client, and a silent no-op would otherwise look like success.
        if action.cidr in self._view.subnets_by_cidr(section_id):
            raise TargetError(
                f"deleted subnet {action.cidr} but it is still present on "
                f"re-read of section id {section_id} -- treating as a "
                f"failed write"
            )

    def _do_update_address(self, action):
        subnet_id = self._subnet_id(action.section, action.cidr)
        existing = self._view.addresses_by_ip(subnet_id).get(action.ip)
        if not existing:
            raise TargetError(
                f"address {action.ip} vanished from {action.cidr} between "
                f"plan and apply"
            )
        self._client.update_address(existing["id"], **action.detail)
