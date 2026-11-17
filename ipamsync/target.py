"""
ipamsync.target

Reads the target instance's current state and executes a plan against it.

TargetView fetches each section's subnets and each subnet's addresses once,
indexed by natural key, and invalidates narrowly after writes.

Creates are confirmed, not assumed: the returned id is ignored and the
record is re-read by natural key, so a silent no-op stops the run.
"""

import re

from ipamsync import model
from ipamsync.model import CUSTOM_FIELD_PREFIX
from phpipam_client import PhpIpamError
from ipamsync.plan import split_cidr
from ipamsync.snapshot import canonical_cidr


class TargetError(Exception):
    """Raised when the target is in a state the importer cannot safely
    proceed from."""


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
        """Section by name, case-insensitively."""
        wanted = str(name).strip().casefold()
        for section in self._load_sections():
            if str(section.get("name", "")).strip().casefold() == wanted:
                return section
        return None

    def invalidate_sections(self):
        self._sections = None

    # -- Subnets -------------------------------------------------------

    def subnets_by_cidr(self, section_id):
        """All subnets in a section (any depth), indexed by canonical CIDR,
        custom fields flattened."""
        if section_id not in self._subnets:
            index = {}
            for raw in self._client.get_subnets_in_section(section_id):
                if not raw.get("subnet") or raw.get("mask") in (None, ""):
                    continue  # folders and malformed rows have no network
                try:
                    key = canonical_cidr(raw["subnet"], raw["mask"])
                except ValueError:
                    continue
                index[key] = model.flatten_custom_fields(raw)
            self._subnets[section_id] = index
        return self._subnets[section_id]

    def invalidate_subnets(self, section_id):
        self._subnets.pop(section_id, None)
        self._folders.pop(section_id, None)

    # -- Folders -------------------------------------------------------
    #
    # A folder is a `subnets` row with isFolder=1, named by `description`.
    # Indexed by path (tuple of names), as names repeat under different
    # parents. Folders nest only under folders.

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
        """Folder path a masterSubnetId points at, or None if not a folder."""
        if str(folder_id or "") in ("", "0", "None"):
            return None
        self.folders_by_path(section_id)
        return self._folders[section_id][1].get(str(folder_id))

    # -- L2 domains, VLANs and VRFs ------------------------------------
    #
    # Indexed by natural key: domain by name, VLAN by (domain name, number),
    # VRF by (section id, name). Reverse maps read a subnet's vlanId/vrfId
    # back as natural keys.

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
        """VRF by (section, name), never by name alone: VRF names are not
        unique across sections."""
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
        """The (domain, number) a vlanId points at, or None."""
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
        """L2 domains scoped to this section and no other (the only VLAN
        deletion candidates). Domain 1 serves every section and is skipped.
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
        """VRFs scoped to this section and no other."""
        return [raw for raw in self._load_l2()["vrfs"]
                if self._section_list(raw) == {str(section_id)}]

    def subnets_under(self, section_id, parent_id):
        """Rows (subnets or folders) whose parent is `parent_id`."""
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
    """Splits fields into (stock, custom), for error messages."""
    stock, custom = {}, {}
    for key, value in fields.items():
        (custom if key.startswith(CUSTOM_FIELD_PREFIX) else stock)[key] = value
    return stock, custom


class Executor:
    """Runs a plan's write actions against the target instance.

    Failures are per-record: errors are collected and the run continues.
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

        # Prefer the column phpIPAM names over guessing from the payload.
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
            # Overlap with a sibling the plan could not detach (typically a
            # new aggregate above existing top-level subnets).
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
        """Deletes an orphaned folder only if it is empty on the target.

        phpIPAM deletes a folder's entire contents with it, so a non-empty
        folder is left for a later run.
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
        # phpIPAM nulls referencing subnets' vlanId; it does not delete them.
        self._client.delete_vlan(vlan["id"])
        self._view.invalidate_l2()

    def _do_delete_vrf(self, action):
        vrf = self._view.vrf_by_name(action.key, self._section_id(action.section))
        if not vrf:
            return
        self._client.delete_vrf(vrf["id"])
        self._view.invalidate_l2()

    def _do_create_folder(self, action):
        """Creates one folder (a subnets row with isFolder=1, name in
        `description`), under its parent folder if it has one."""
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
        """Adds the target section to an existing domain's section list
        (never replaces it: the domain may serve other sources)."""
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
        """Points a subnet at its VLAN and/or VRF, resolving natural keys
        to local ids, in one PATCH."""
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
        """Moves a subnet to the top level so its new parent can be created
        without a sibling overlap. A reparent_subnet follows in the plan."""
        section_id = self._section_id(action.section)
        subnet_id = self._subnet_id(action.section, action.cidr)
        self._client.update_subnet(subnet_id, masterSubnetId=0)
        self._view.invalidate_subnets(section_id)

    def _do_reparent_subnet(self, action):
        """Moves a subnet under a different parent (or to top level),
        resolving the parent's local id at apply time."""
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
            # Top level is 0, not null.
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
        # Confirm by re-read, as for creates.
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
