"""
End-to-end round trip: source -> snapshot -> target, using the real exporter
and importer against an in-memory fake phpIPAM.

Checks that the importer accepts what the exporter writes, that ids are
resolved by natural key, and that a second run is a no-op. The fake returns
every field as a string, as phpIPAM does. It models the API as this tool
understands it, so it cannot prove that understanding against a real instance.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ipam_export  # noqa: E402
from ipamsync.plan import build_plan, canonicalise_document_cidr  # noqa: E402
from ipamsync.snapshot import partition_documents, read_snapshot  # noqa: E402
from ipamsync.target import Executor, TargetView  # noqa: E402


class FakePhpIpam:
    """In-memory stand-in for the subset of phpIPAM this tool uses.
    Returns every field as a string, like the real API."""

    def __init__(self, custom_subnet_fields=(), custom_address_fields=(),
                 first_id=1):
        self.sections = []
        self.subnets = []
        self.addresses = []
        self.vlans = []
        self.vrfs = []
        # Domain 1 ("default") always exists and belongs to every section.
        self.l2domains = [{"id": "1", "name": "default",
                           "description": "default L2 domain",
                           "sections": ""}]
        # Targets use a different id counter so ids never match by accident.
        self._next_id = first_id
        self._next_domain_id = 40
        self.write_calls = []
        # Custom-field column names.
        self.custom_subnet_fields = set(custom_subnet_fields)
        self.custom_address_fields = set(custom_address_fields)

    def _present(self, record, names, always_nest):
        """Reproduce phpIPAM's custom-field serialisation.

        A single-record read always nests. A list read nests only when at
        least one custom field is non-null; otherwise the raw columns appear
        at top level, indistinguishable from standard fields.
        """
        if not names:
            return dict(record)
        plain = {k: v for k, v in record.items() if k not in names}
        values = {name: record.get(name) for name in names}
        if always_nest or any(v not in (None, "") for v in values.values()):
            plain["custom_fields"] = values
        else:
            plain.update(values)
        return plain

    # -- helpers used by tests, not part of the client interface --------

    def _new_id(self):
        value = self._next_id
        self._next_id += 1
        return str(value)

    def add_section(self, name):
        section = {"id": self._new_id(), "name": name}
        self.sections.append(section)
        return section["id"]

    def add_subnet(self, section_id, subnet, mask, master_subnet_id=None, **fields):
        record = {
            "id": self._new_id(),
            "subnet": subnet,
            "mask": str(mask),
            "sectionId": str(section_id),
            "masterSubnetId": str(master_subnet_id) if master_subnet_id else "0",
        }
        record.update({key: str(value) for key, value in fields.items()})
        self.subnets.append(record)
        return record["id"]

    def add_address(self, subnet_id, ip, **fields):
        record = {"id": self._new_id(), "subnetId": str(subnet_id), "ip": ip}
        record.update({key: str(value) for key, value in fields.items()})
        self.addresses.append(record)
        return record["id"]

    def add_folder(self, section_id, name, master_subnet_id=None):
        """A folder: a subnets row with isFolder=1, no network, and its
        name in `description`."""
        record = {
            "id": self._new_id(),
            "subnet": "0.0.0.0",
            "mask": None,
            "sectionId": str(section_id),
            "masterSubnetId": str(master_subnet_id) if master_subnet_id else "0",
            "description": name,
            "isFolder": "1",
        }
        self.subnets.append(record)
        return record["id"]

    def add_vlan(self, number, name, domain_id="1"):
        vlan = {"id": self._new_id(), "number": str(number), "name": name,
                "domainId": str(domain_id), "description": ""}
        self.vlans.append(vlan)
        return vlan["id"]

    def add_l2domain(self, name, sections=None, domain_id=None):
        # Separate id space, so a created domain never collides with id 1.
        if domain_id is None:
            self._next_domain_id += 1
            domain_id = self._next_domain_id
        domain = {"id": str(domain_id), "name": name,
                  "description": "",
                  "sections": ";".join(str(s) for s in (sections or []))}
        self.l2domains.append(domain)
        return domain["id"]

    def add_vrf(self, name, sections=None, rd="", description=""):
        vrf = {"id": self._new_id(), "name": name, "rd": rd,
               "description": description,
               "sections": ";".join(str(s) for s in (sections or []))}
        self.vrfs.append(vrf)
        return vrf["id"]

    def subnet_by_cidr(self, cidr):
        network, mask = cidr.split("/")
        for record in self.subnets:
            if record["subnet"] == network and record["mask"] == mask:
                return record
        return None

    # -- the PhpIpamClient interface ------------------------------------

    def get_sections(self):
        return [dict(section) for section in self.sections]

    def get_subnets_in_section(self, section_id):
        # phpIPAM sorts folders first (`order by isFolder desc`).
        rows = [record for record in self.subnets
                if record["sectionId"] == str(section_id)]
        rows.sort(key=lambda r: str(r.get("isFolder") or "0") != "1")
        return [self._present(record, self.custom_subnet_fields, False)
                for record in rows]

    def get_subnet(self, subnet_id):
        for record in self.subnets:
            if record["id"] == str(subnet_id):
                if str(record.get("isFolder") or "0") == "1":
                    # GET subnets/{id}/ on a folder is 404 "No subnets found".
                    return None
                return self._present(record, self.custom_subnet_fields, True)
        return None

    def get_addresses_in_subnet(self, subnet_id):
        return [self._present(record, self.custom_address_fields, False)
                for record in self.addresses
                if record["subnetId"] == str(subnet_id)]

    def get_address(self, address_id):
        for record in self.addresses:
            if record["id"] == str(address_id):
                return self._present(record, self.custom_address_fields, True)
        return None

    def get_l2domains(self):
        # Domain 1 always exists in phpIPAM and belongs to every section.
        return [dict(d) for d in self.l2domains]

    def get_vlans(self):
        return [dict(v) for v in self.vlans]

    def get_vrfs(self):
        return [dict(v) for v in self.vrfs]

    def create_l2domain(self, name, *, sections=None, **fields):
        self.write_calls.append(("create_l2domain", name))
        return self.add_l2domain(name, sections=sections or [])

    def update_l2domain(self, domain_id, *, sections=None, **fields):
        self.write_calls.append(("update_l2domain", str(domain_id)))
        for domain in self.l2domains:
            if domain["id"] == str(domain_id):
                domain.update({k: str(v) for k, v in fields.items()})
                if sections is not None:
                    domain["sections"] = ";".join(str(s) for s in sections)
                return
        raise AssertionError(f"update_l2domain on unknown id {domain_id}")

    def create_vlan(self, *, number, name, domain_id, **fields):
        self.write_calls.append(("create_vlan", f"{domain_id}/{number}"))
        new_id = self.add_vlan(number, name, domain_id=domain_id)
        for vlan in self.vlans:
            if vlan["id"] == new_id:
                vlan.update({k: str(v) for k, v in fields.items()})
        return new_id

    def update_vlan(self, vlan_id, **fields):
        self.write_calls.append(("update_vlan", str(vlan_id)))
        for vlan in self.vlans:
            if vlan["id"] == str(vlan_id):
                vlan.update({k: str(v) for k, v in fields.items()
                             if k not in ("domain",)})
                return
        raise AssertionError(f"update_vlan on unknown id {vlan_id}")

    def create_vrf(self, *, name, sections=None, **fields):
        self.write_calls.append(("create_vrf", name))
        new_id = self.add_vrf(name, sections=sections or [])
        for vrf in self.vrfs:
            if vrf["id"] == new_id:
                vrf.update({k: str(v) for k, v in fields.items()})
        return new_id

    def update_vrf(self, vrf_id, *, sections=None, **fields):
        self.write_calls.append(("update_vrf", str(vrf_id)))
        for vrf in self.vrfs:
            if vrf["id"] == str(vrf_id):
                vrf.update({k: str(v) for k, v in fields.items()})
                if sections is not None:
                    vrf["sections"] = ";".join(str(s) for s in sections)
                return
        raise AssertionError(f"update_vrf on unknown id {vrf_id}")

    def get_vlan(self, vlan_id):
        for vlan in self.vlans:
            if vlan["id"] == str(vlan_id):
                return dict(vlan)
        return None

    def create_section(self, name, **fields):
        self.write_calls.append(("create_section", name))
        return self.add_section(name)

    def create_subnet(self, *, subnet, mask, section_id, description=None, **extra):
        self.write_calls.append(("create_subnet", f"{subnet}/{mask}"))
        fields = dict(extra)
        if description is not None:
            fields["description"] = description
        master = fields.pop("masterSubnetId", None)
        return self.add_subnet(section_id, subnet, mask,
                               master_subnet_id=master, **fields)

    def update_subnet(self, subnet_id, **fields):
        self.write_calls.append(("update_subnet", str(subnet_id)))
        for record in self.subnets:
            if record["id"] == str(subnet_id):
                record.update({k: str(v) for k, v in fields.items()})
                return
        raise AssertionError(f"update_subnet on unknown id {subnet_id}")

    def create_address(self, *, subnet_id, ip=None, hostname=None,
                       description=None, **extra):
        self.write_calls.append(("create_address", f"{subnet_id}/{ip}"))
        fields = dict(extra)
        if hostname is not None:
            fields["hostname"] = hostname
        if description is not None:
            fields["description"] = description
        return self.add_address(subnet_id, ip, **fields)

    def update_address(self, address_id, **fields):
        self.write_calls.append(("update_address", str(address_id)))
        for record in self.addresses:
            if record["id"] == str(address_id):
                record.update({k: str(v) for k, v in fields.items()})
                return
        raise AssertionError(f"update_address on unknown id {address_id}")


def make_config(sections=("Shared",)):
    return {
        "source": {"base_url": "https://ipam1.test", "app_id": "sync"},
        "target": {"base_url": "https://ipam2.test", "app_id": "sync"},
        "sections": list(sections),
        "section_map": {},
        "options": {"sync_tags": True, "create_missing_sections": False},
    }


def populated_source():
    """A source with a nested subnet, a custom field, a VLAN, and an
    unknown field."""
    source = FakePhpIpam()
    section_id = source.add_section("Shared")
    vlan_id = source.add_vlan(100, "shared-vlan")

    parent = source.add_subnet(
        section_id, "10.20.0.0", 16, description="Shared supernet", showName="1",
    )
    child = source.add_subnet(
        section_id, "10.20.5.0", 24, master_subnet_id=parent,
        description="Shared /24", vlanId=vlan_id, custom_Owner="netops",
        somethingUnknown="ignore me",
    )
    source.add_address(child, "10.20.5.10", hostname="sw10",
                       description="access switch", mac="aa:bb:cc:dd:ee:01")
    source.add_address(child, "10.20.5.2", hostname="gw",
                       description="gateway", is_gateway="1")
    return source, section_id


def run_export(source, out_dir, config):
    """Runs the real exporter with the fake client injected."""
    original = ipam_export.build_client
    ipam_export.build_client = lambda cfg, side: source
    try:
        return ipam_export.export(config, out_dir)
    finally:
        ipam_export.build_client = original


def run_import(target, snapshot_dir, config, apply=True):
    """Plan (and optionally apply) against the fake target, as
    ipam_import.main() does."""
    _, documents = read_snapshot(snapshot_dir)
    documents = [canonicalise_document_cidr(d) for d in documents]
    view = TargetView(target)
    actions = build_plan(documents, view, config)
    if apply:
        executor = Executor(target, view, config, lambda message: None)
        applied, errors = executor.apply(actions)
        return actions, applied, errors
    return actions, 0, []


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.source, _ = populated_source()
        self.target = FakePhpIpam(first_id=500)
        self.target.add_section("Shared")
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot_dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _sync(self, apply=True):
        run_export(self.source, self.snapshot_dir, self.config)
        return run_import(self.target, self.snapshot_dir, self.config, apply=apply)

    def test_export_writes_expected_snapshot(self):
        manifest = run_export(self.source, self.snapshot_dir, self.config)
        self.assertEqual(manifest["subnet_count"], 2)
        self.assertEqual(manifest["address_count"], 2)
        # The unknown source field must be reported, not silently dropped.
        self.assertIn(
            "somethingUnknown", manifest["dropped_source_fields"]["subnet"],
        )

    def test_first_import_creates_everything(self):
        actions, applied, errors = self._sync()
        self.assertEqual(errors, [])
        kinds = [a.kind for a in actions if a.is_write]
        self.assertEqual(kinds.count("create_subnet"), 2)
        self.assertEqual(kinds.count("create_address"), 2)
        self.assertEqual(applied, 6)

    def test_target_matches_source_after_import(self):
        self._sync()
        child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.assertIsNotNone(child)
        self.assertEqual(child["description"], "Shared /24")
        self.assertEqual(child["custom_Owner"], "netops")

        ips = sorted(a["ip"] for a in self.target.get_addresses_in_subnet(child["id"]))
        self.assertEqual(ips, ["10.20.5.10", "10.20.5.2"])

    def test_nesting_is_reconstructed_by_natural_key(self):
        self._sync()
        parent = self.target.subnet_by_cidr("10.20.0.0/16")
        child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.assertEqual(child["masterSubnetId"], parent["id"])
        # Not the source's id: ids are instance-local.
        source_parent = self.source.subnet_by_cidr("10.20.0.0/16")
        self.assertNotEqual(child["masterSubnetId"], source_parent["id"])

    def test_local_ids_are_not_copied_across(self):
        self._sync()
        source_child = self.source.subnet_by_cidr("10.20.5.0/24")
        target_child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.assertNotEqual(source_child["id"], target_child["id"])
        # The target's own VLAN id, not the source's.
        target_vlan = next(v for v in self.target.vlans
                           if v["number"] == "100")
        self.assertEqual(target_child["vlanId"], target_vlan["id"])
        self.assertNotEqual(target_child["vlanId"], source_child["vlanId"])

    def test_second_import_is_a_no_op(self):
        # Steady state must produce no writes.
        self._sync()
        self.target.write_calls.clear()
        actions, applied, errors = self._sync()
        self.assertEqual([a for a in actions if a.is_write], [])
        self.assertEqual(applied, 0)
        self.assertEqual(self.target.write_calls, [])

    def test_a_source_change_propagates_as_a_single_update(self):
        self._sync()
        self.target.write_calls.clear()

        source_child = self.source.subnet_by_cidr("10.20.5.0/24")
        source_child["description"] = "Shared /24 (renamed)"

        actions, applied, errors = self._sync()
        writes = [a for a in actions if a.is_write]
        self.assertEqual([a.kind for a in writes], ["update_subnet"])
        self.assertEqual(applied, 1)
        self.assertEqual(
            self.target.subnet_by_cidr("10.20.5.0/24")["description"],
            "Shared /24 (renamed)",
        )

    def test_a_new_source_address_propagates(self):
        self._sync()
        child = self.source.subnet_by_cidr("10.20.5.0/24")
        self.source.add_address(child["id"], "10.20.5.99", hostname="new-host")

        actions, applied, errors = self._sync()
        writes = [a for a in actions if a.is_write]
        self.assertEqual([a.kind for a in writes], ["create_address"])
        target_child = self.target.subnet_by_cidr("10.20.5.0/24")
        ips = {a["ip"] for a in self.target.get_addresses_in_subnet(target_child["id"])}
        self.assertIn("10.20.5.99", ips)

    def test_source_deletion_is_reported_but_not_applied(self):
        # Additive-only: the record survives and shows up as drift.
        self._sync()
        source_child = self.source.subnet_by_cidr("10.20.5.0/24")
        self.source.addresses = [
            a for a in self.source.addresses
            if not (a["subnetId"] == source_child["id"] and a["ip"] == "10.20.5.10")
        ]

        actions, applied, errors = self._sync()
        drift = [a for a in actions if a.kind == "drift_address"]
        self.assertEqual([a.ip for a in drift], ["10.20.5.10"])
        self.assertEqual(applied, 0)

        target_child = self.target.subnet_by_cidr("10.20.5.0/24")
        ips = {a["ip"] for a in self.target.get_addresses_in_subnet(target_child["id"])}
        self.assertIn("10.20.5.10", ips, "additive importer must not delete")

    def test_target_only_record_is_left_alone(self):
        self._sync()
        target_child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.target.add_address(target_child["id"], "10.20.5.200", hostname="local")

        actions, applied, errors = self._sync()
        drift = [a for a in actions if a.kind == "drift_address"]
        self.assertEqual([a.ip for a in drift], ["10.20.5.200"])
        ips = {a["ip"] for a in self.target.get_addresses_in_subnet(target_child["id"])}
        self.assertIn("10.20.5.200", ips)

    def test_dry_run_writes_nothing(self):
        run_export(self.source, self.snapshot_dir, self.config)
        actions, applied, errors = run_import(
            self.target, self.snapshot_dir, self.config, apply=False,
        )
        self.assertTrue([a for a in actions if a.is_write],
                        "expected a non-empty plan for this assertion to mean anything")
        self.assertEqual(self.target.write_calls, [])
        self.assertIsNone(self.target.subnet_by_cidr("10.20.5.0/24"))


class TestCustomFieldDiscovery(unittest.TestCase):
    """List endpoints omit `custom_fields` for records whose custom fields
    are all null, leaking the raw columns at top level. Those must still be
    treated as custom fields, and a value cleared upstream must propagate.
    """

    def _source(self):
        source = FakePhpIpam(custom_subnet_fields={"Owner"})
        section_id = source.add_section("Shared")
        # One subnet with a value (nests in the list) and one without
        # (leaks `Owner` at top level).
        source.add_subnet(section_id, "10.20.1.0", 24,
                          description="has owner", Owner="netops-team")
        source.add_subnet(section_id, "10.20.2.0", 24,
                          description="no owner")
        return source

    def _export(self, source, tmp):
        return run_export(source, tmp, make_config())

    def test_all_null_record_is_not_reported_as_an_unknown_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._export(self._source(), tmp)
            self.assertEqual(
                manifest["dropped_source_fields"], {},
                "the all-null custom field was misreported as schema drift",
            )

    def test_custom_field_is_carried_for_both_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._export(self._source(), tmp)
            _, documents = read_snapshot(tmp)
            by_cidr = {d["cidr"]: d for d in documents
                       if d.get("kind") != "section"}
            self.assertEqual(by_cidr["10.20.1.0/24"]["fields"]["Owner"],
                             "netops-team")
            # Present and empty, not absent -- so clearing propagates.
            self.assertEqual(by_cidr["10.20.2.0/24"]["fields"]["Owner"], "")

    def test_clearing_a_custom_field_upstream_propagates(self):
        config = make_config()
        source = self._source()
        target = FakePhpIpam(custom_subnet_fields={"Owner"})
        target.add_section("Shared")

        with tempfile.TemporaryDirectory() as tmp:
            self._export(source, tmp)
            run_import(target, tmp, config)
            self.assertEqual(
                target.subnet_by_cidr("10.20.1.0/24")["Owner"], "netops-team")

            # Clear it upstream, so its list entry stops nesting.
            source.subnet_by_cidr("10.20.1.0/24")["Owner"] = ""
            self._export(source, tmp)
            actions, applied, errors = run_import(target, tmp, config)

            self.assertEqual(errors, [])
            self.assertEqual(
                target.subnet_by_cidr("10.20.1.0/24")["Owner"], "",
                "clearing a custom field upstream did not propagate",
            )

    def test_steady_state_is_still_a_no_op_with_custom_fields(self):
        config = make_config()
        source = self._source()
        target = FakePhpIpam(custom_subnet_fields={"Owner"})
        target.add_section("Shared")
        with tempfile.TemporaryDirectory() as tmp:
            self._export(source, tmp)
            run_import(target, tmp, config)
            target.write_calls.clear()
            self._export(source, tmp)
            actions, applied, errors = run_import(target, tmp, config)
            self.assertEqual(applied, 0)
            self.assertEqual(target.write_calls, [])


class TestFailedWriteIsContained(unittest.TestCase):
    def test_a_rejected_record_does_not_stop_the_run(self):
        config = make_config()
        source, _ = populated_source()
        target = FakePhpIpam(first_id=500)
        target.add_section("Shared")

        # Reject one address as phpIPAM would an unknown custom field; the
        # other records must still land.
        original = target.create_address

        def flaky(*, subnet_id, ip=None, **kwargs):
            if ip == "10.20.5.10":
                raise RuntimeError("Custom field custom_Owner does not exist")
            return original(subnet_id=subnet_id, ip=ip, **kwargs)

        target.create_address = flaky

        with tempfile.TemporaryDirectory() as tmp:
            run_export(source, tmp, config)
            actions, applied, errors = run_import(target, tmp, config)

        self.assertEqual(len(errors), 1)
        self.assertIn("10.20.5.10", errors[0])
        # The other three writes (two subnets, one address) still happened.
        self.assertEqual(applied, 5)
        child = target.subnet_by_cidr("10.20.5.0/24")
        ips = {a["ip"] for a in target.get_addresses_in_subnet(child["id"])}
        self.assertEqual(ips, {"10.20.5.2"})


class TestReparenting(unittest.TestCase):
    """A subnet re-parented at the source is re-parented on the target.
    `masterSubnetId` is instance-local and not a compared field."""

    def setUp(self):
        self.config = make_config()
        self.source, self.section_id = populated_source()
        self.target = FakePhpIpam(first_id=500)
        self.target.add_section("Shared")
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot_dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _sync(self):
        run_export(self.source, self.snapshot_dir, self.config)
        return run_import(self.target, self.snapshot_dir, self.config, apply=True)

    def test_moving_a_subnet_under_a_new_parent_moves_it_on_the_target(self):
        self._sync()
        original_parent = self.target.subnet_by_cidr("10.20.0.0/16")

        new_parent = self.source.add_subnet(
            self.section_id, "10.20.4.0", 22, description="new supernet",
        )
        self.source.subnet_by_cidr("10.20.5.0/24")["masterSubnetId"] = str(new_parent)

        actions, applied, errors = self._sync()
        self.assertEqual(errors, [])
        self.assertIn("reparent_subnet", [action.kind for action in actions])

        child = self.target.subnet_by_cidr("10.20.5.0/24")
        parent = self.target.subnet_by_cidr("10.20.4.0/22")
        self.assertEqual(child["masterSubnetId"], parent["id"])
        # and specifically no longer under the old one
        self.assertNotEqual(child["masterSubnetId"], original_parent["id"])

    def test_moving_a_subnet_to_the_top_level_clears_its_parent(self):
        self._sync()
        self.source.subnet_by_cidr("10.20.5.0/24")["masterSubnetId"] = "0"

        actions, applied, errors = self._sync()
        self.assertEqual(errors, [])
        child = self.target.subnet_by_cidr("10.20.5.0/24")
        # phpIPAM spells "top level" as 0, not null.
        self.assertEqual(child["masterSubnetId"], "0")

    def test_an_unchanged_parent_produces_no_action(self):
        """Negative control: an unchanged parent must not re-parent every
        nested subnet on each run."""
        self._sync()
        actions, applied, errors = self._sync()
        self.assertEqual([a.kind for a in actions if a.is_write], [])
        self.assertEqual(applied, 0)

    def test_a_parent_outside_the_replicated_section_flattens_to_top_level(self):
        """A parent outside the replicated sections is exported as no
        parent, so the subnet moves to the top level of its section."""
        self._sync()

        other_section = self.source.add_section("LocalOnly")
        outside = self.source.add_subnet(other_section, "10.99.0.0", 16)
        self.source.subnet_by_cidr("10.20.5.0/24")["masterSubnetId"] = str(outside)

        actions, applied, errors = self._sync()
        self.assertEqual(errors, [])
        child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.assertEqual(child["masterSubnetId"], "0")


class TestFolders(unittest.TestCase):
    """Folders (subnets rows with isFolder=1, no network) replicate, and
    subnets inside them keep their place."""

    def setUp(self):
        self.config = make_config()
        self.source, self.section_id = populated_source()
        self.dc = self.source.add_folder(self.section_id, "Datacentre")
        self.rack = self.source.add_folder(self.section_id, "Rack A",
                                           master_subnet_id=self.dc)
        # An empty folder, which must replicate anyway.
        self.source.add_folder(self.section_id, "Spare")
        self.source.subnet_by_cidr("10.20.5.0/24")["masterSubnetId"] = self.rack

        self.target = FakePhpIpam(first_id=500)
        self.target.add_section("Shared")
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot_dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def _sync(self):
        run_export(self.source, self.snapshot_dir, self.config)
        return run_import(self.target, self.snapshot_dir, self.config, apply=True)

    def _target_folder(self, name):
        for record in self.target.subnets:
            if (str(record.get("isFolder") or "0") == "1"
                    and record.get("description") == name):
                return record
        return None

    def test_folders_replicate_including_empty_ones(self):
        self._sync()
        self.assertIsNotNone(self._target_folder("Datacentre"))
        self.assertIsNotNone(self._target_folder("Rack A"))
        self.assertIsNotNone(self._target_folder("Spare"),
                             "an empty folder must still replicate")

    def test_folder_nesting_is_rebuilt_by_path(self):
        self._sync()
        dc = self._target_folder("Datacentre")
        rack = self._target_folder("Rack A")
        self.assertEqual(rack["masterSubnetId"], dc["id"])
        # and not the source's id for it
        self.assertNotEqual(rack["masterSubnetId"], self.dc)

    def test_a_subnet_inside_a_folder_lands_inside_it(self):
        self._sync()
        rack = self._target_folder("Rack A")
        child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.assertEqual(child["masterSubnetId"], rack["id"])
        self.assertNotEqual(child["masterSubnetId"], self.rack)

    def test_a_folder_is_not_exported_as_a_subnet(self):
        run_export(self.source, self.snapshot_dir, self.config)
        manifest, documents = read_snapshot(self.snapshot_dir)
        subnets, sections = partition_documents(documents)
        self.assertEqual(manifest["subnet_count"], len(subnets))
        self.assertNotIn(None, [d.get("cidr") for d in subnets])
        paths = sections[0]["folders"]
        self.assertIn(["Datacentre"], paths)
        self.assertIn(["Datacentre", "Rack A"], paths)
        # Parents before children, which is the order they must be made in.
        self.assertLess(paths.index(["Datacentre"]),
                        paths.index(["Datacentre", "Rack A"]))

    def test_moving_a_subnet_out_of_a_folder_moves_it_on_the_target(self):
        self._sync()
        self.source.subnet_by_cidr("10.20.5.0/24")["masterSubnetId"] = "0"
        actions, applied, errors = self._sync()
        self.assertEqual(errors, [])
        self.assertIn("reparent_subnet", [a.kind for a in actions])
        self.assertEqual(
            self.target.subnet_by_cidr("10.20.5.0/24")["masterSubnetId"], "0")

    def test_custom_fields_still_discovered_when_a_folder_sorts_first(self):
        """Folders sort first and GET subnets/{id} on one is 404, so the
        custom-field probe must skip them."""
        source = FakePhpIpam(custom_subnet_fields={"Owner"})
        section = source.add_section("Shared")
        source.add_folder(section, "Datacentre")
        # Owner is null, so phpIPAM leaks it flat rather than nesting it.
        source.add_subnet(section, "10.20.9.0", 24, description="plain",
                          Owner="")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_export(source, tmp, make_config())
        self.assertNotIn("Owner",
                         manifest["dropped_source_fields"].get("subnet", []))

    def test_steady_state_with_folders_is_a_no_op(self):
        """Negative control: folder paths must not cause churn on a
        steady-state run."""
        self._sync()
        actions, applied, errors = self._sync()
        self.assertEqual([a.kind for a in actions if a.is_write], [])
        self.assertEqual(applied, 0)


if __name__ == "__main__":
    unittest.main()
