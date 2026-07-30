"""
End-to-end round trip: source instance -> snapshot -> target instance.

This drives the real exporter and the real importer against an in-memory
fake phpIPAM, with a temporary directory standing in for the mirrored git
repo. It is the closest thing to a live test that can run without two
real instances, and it exercises the parts unit tests cannot: that what
the exporter writes is what the importer expects, that ids are resolved
by natural key across the boundary, and -- most importantly -- that a
second run is a no-op.

## Why idempotency is the headline assertion

This tool is meant to run on a cron. If a steady-state run rewrote every
record, the target's edit history would fill with meaningless changes,
every run would cost hundreds of API writes, and a genuine change would
be impossible to spot in the logs. `test_second_import_is_a_no_op` is
the test that would catch that, and it is the one to look at first if
this tool ever starts behaving oddly in production.

The fake deliberately returns every field as a *string*, the way phpIPAM
itself does, so the type-normalisation in ipamsync.model is genuinely
under test rather than being bypassed by tidy Python types.

## What this does NOT prove

The fake implements phpIPAM's API as this tool believes it to work. It
cannot confirm that belief. Field names on create, the response shape of
a POST, and whether a PATCH accepts a given field are all still
unverified against a real instance -- see README.md's "Before you trust
this" section. What these tests do prove is that the tool's own logic is
coherent, idempotent, and additive.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ipam_export  # noqa: E402
from ipamsync.plan import build_plan, canonicalise_document_cidr  # noqa: E402
from ipamsync.snapshot import read_snapshot  # noqa: E402
from ipamsync.target import Executor, TargetView  # noqa: E402


class FakePhpIpam:
    """Minimal in-memory stand-in for a phpIPAM instance.

    Mirrors the subset of PhpIpamClient this tool uses, and imitates the
    real API's habit of returning everything as a string.
    """

    def __init__(self, custom_subnet_fields=(), custom_address_fields=()):
        self.sections = []
        self.subnets = []
        self.addresses = []
        self.vlans = []
        self._next_id = 1
        self.write_calls = []
        # Custom-field column names, as phpIPAM would have them: plain
        # extra columns whose names the admin chose.
        self.custom_subnet_fields = set(custom_subnet_fields)
        self.custom_address_fields = set(custom_address_fields)

    def _present(self, record, names, always_nest):
        """Reproduces phpIPAM 1.8.1's custom-field serialisation, quirk
        and all.

        A *single-record* read always nests, even when every custom field
        is null. A *list* read nests only when at least one is non-null;
        otherwise it omits the block entirely and leaks the raw columns
        at top level, where they are indistinguishable from standard
        fields. Modelling that faithfully is the whole point -- the
        earlier fake nested unconditionally, which is why this never
        showed up until a real instance was involved.
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

    def add_vlan(self, number, name):
        vlan = {"id": self._new_id(), "number": str(number), "name": name}
        self.vlans.append(vlan)
        return vlan["id"]

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
        return [self._present(record, self.custom_subnet_fields, False)
                for record in self.subnets
                if record["sectionId"] == str(section_id)]

    def get_subnet(self, subnet_id):
        for record in self.subnets:
            if record["id"] == str(subnet_id):
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
    """A source instance with the awkward cases: a nested subnet, a
    custom field, a VLAN, and a field this tool does not know about."""
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
    """Reads the snapshot and plans (and optionally applies) against the
    fake target -- the same call sequence ipam_import.main() uses."""
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
        self.target = FakePhpIpam()
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
        self.assertEqual(applied, 4)

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
        # And crucially NOT the source's id for that parent -- ids must
        # never be carried across instances.
        source_parent = self.source.subnet_by_cidr("10.20.0.0/16")
        self.assertNotEqual(child["masterSubnetId"], source_parent["id"])

    def test_local_ids_are_not_copied_across(self):
        self._sync()
        source_child = self.source.subnet_by_cidr("10.20.5.0/24")
        target_child = self.target.subnet_by_cidr("10.20.5.0/24")
        self.assertNotEqual(source_child["id"], target_child["id"])
        # The source's vlanId must not have been pasted onto the target,
        # where it would point at an unrelated VLAN row.
        self.assertIn(target_child.get("vlanId", "0"), ("0", None, ""))

    def test_second_import_is_a_no_op(self):
        # The headline assertion -- see this module's docstring.
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
        # Additive-only: the record must survive on the target and show
        # up as drift.
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
    """phpIPAM's list endpoints omit the `custom_fields` block for records
    whose custom fields are all null, leaking the raw columns at top level
    instead (confirmed on 1.8.1). Those records must still be handled as
    custom rather than reported as unknown fields.

    Two things go wrong without the single-record discovery probe:
    the dropped-field report fires on every export for perfectly ordinary
    data -- training you to ignore the report that exists to warn you
    about genuine schema drift -- and a custom field CLEARED upstream
    never propagates, because the cleared record no longer nests.
    """

    def _source(self):
        source = FakePhpIpam(custom_subnet_fields={"Owner"})
        section_id = source.add_section("Shared")
        # One subnet WITH a value (so its list entry nests) and one
        # without (so its list entry leaks `Owner` at top level).
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
            by_cidr = {d["cidr"]: d for d in documents}
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

            # Clear it upstream: the source record now has no non-null
            # custom field, so its list entry stops nesting.
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
        target = FakePhpIpam()
        target.add_section("Shared")

        # Reject one address the way phpIPAM would reject an unknown
        # custom field, and confirm the other records still land.
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
        self.assertEqual(applied, 3)
        child = target.subnet_by_cidr("10.20.5.0/24")
        ips = {a["ip"] for a in target.get_addresses_in_subnet(child["id"])}
        self.assertEqual(ips, {"10.20.5.2"})


if __name__ == "__main__":
    unittest.main()
