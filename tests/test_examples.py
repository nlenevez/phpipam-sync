"""
Verifies the committed example snapshot in examples/.

Reads it through the importer's code path (which validates checksums and
the schema version) and asserts what examples/README.md describes. Fails
if the snapshot format changes without the examples being regenerated.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync.plan import build_plan, canonicalise_document_cidr  # noqa: E402
from ipamsync.snapshot import (  # noqa: E402
    SCHEMA_VERSION, find_stale_files, partition_documents, read_snapshot,
)
from test_plan import FakeTarget, config  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "snapshot"


class TestExampleSnapshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # read_snapshot verifies every checksum and the schema version.
        cls.manifest, all_documents = read_snapshot(EXAMPLES)
        cls.documents, cls.section_docs = partition_documents(all_documents)
        cls.by_cidr = {d["cidr"]: d for d in cls.documents}

    def test_snapshot_is_intact_and_complete(self):
        self.assertEqual(self.manifest["subnet_count"], len(self.documents))
        self.assertEqual(
            self.manifest["address_count"],
            sum(len(d["addresses"]) for d in self.documents),
        )
        self.assertEqual(find_stale_files(EXAMPLES, self.manifest), [])

    def test_manifest_checksums_cover_every_file(self):
        on_disk = {
            str(p.relative_to(EXAMPLES))
            for p in EXAMPLES.rglob("*.json")
            if p.name != "manifest.json"
        }
        self.assertEqual(set(self.manifest["files"]), on_disk)

    def test_field_policy_covered_this_instance(self):
        # The README says the report is empty; update both together.
        self.assertEqual(self.manifest["dropped_source_fields"], {})

    # -- the cases the README points at --------------------------------

    def test_supernet_has_no_addresses(self):
        # A supernet with no addresses: phpIPAM returns 404 for the
        # empty collection.
        self.assertEqual(self.by_cidr["10.20.0.0/16"]["addresses"], [])

    def test_nesting_is_recorded_by_parent_cidr_not_id(self):
        document = self.by_cidr["10.20.5.0/24"]
        self.assertEqual(document["master_subnet"], "10.20.0.0/16")
        self.assertIn(document["master_subnet"], self.by_cidr)

    def test_standalone_subnet_has_no_parent(self):
        self.assertIsNone(self.by_cidr["10.30.0.0/24"]["master_subnet"])

    def test_vlan_is_recorded_but_not_a_field(self):
        document = self.by_cidr["10.20.5.0/24"]
        self.assertEqual(document["vlan"]["number"], "100")
        self.assertNotIn("vlanId", document["fields"])

    def test_custom_fields_are_flat_prefixed_or_not(self):
        fields = self.by_cidr["10.20.5.0/24"]["fields"]
        self.assertEqual(fields["Owner"], "netops-team")       # no prefix
        self.assertEqual(fields["custom_Notes"], "prefixed note")
        self.assertNotIn("custom_fields", fields)              # never nested

    def test_address_custom_field_is_present_even_when_empty(self):
        # Empty rather than absent, so clearing one upstream propagates.
        addresses = {a["ip"]: a["fields"]
                     for a in self.by_cidr["10.20.5.0/24"]["addresses"]}
        self.assertEqual(addresses["10.20.5.2"]["AssetTag"], "ASSET-0042")
        self.assertEqual(addresses["10.20.5.10"]["AssetTag"], "")

    def test_subnet_tag_uses_the_read_name(self):
        # Subnets read as 'tag' but written as 'state'; the snapshot stores
        # the read name.
        self.assertEqual(self.by_cidr["10.20.5.0/24"]["fields"]["tag"], "2")
        self.assertNotIn("state", self.by_cidr["10.20.5.0/24"]["fields"])

    def test_addresses_are_in_numeric_ip_order(self):
        ips = [a["ip"] for a in self.by_cidr["10.20.5.0/24"]["addresses"]]
        self.assertEqual(ips, ["10.20.5.2", "10.20.5.10", "10.20.5.20"])

    def test_ipv6_is_represented_identically(self):
        document = self.by_cidr["2001:db8:5::/64"]
        self.assertEqual(document["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(document["addresses"]), 1)

    def test_no_database_ids_anywhere(self):
        # Additive-only, asserted against the committed snapshot.
        banned = {"id", "subnetId", "sectionId", "masterSubnetId", "vlanId",
                  "gatewayId", "deviceId", "vrfId", "nameserverId"}
        for document in self.documents:
            self.assertEqual(set(document["fields"]) & banned, set(),
                             f"{document['cidr']} carries a database id")
            for address in document["addresses"]:
                self.assertEqual(set(address["fields"]) & banned, set(),
                                 f"{address['ip']} carries a database id")

    def test_every_value_is_a_string(self):
        for document in self.documents:
            for name, value in document["fields"].items():
                self.assertIsInstance(value, str, f"{document['cidr']}.{name}")
            for address in document["addresses"]:
                for name, value in address["fields"].items():
                    self.assertIsInstance(value, str, f"{address['ip']}.{name}")


class TestExamplesAreUsable(unittest.TestCase):
    """The importer can plan from the examples."""

    def test_importer_can_build_a_plan_from_the_examples(self):
        _, documents = read_snapshot(EXAMPLES)
        documents = [canonicalise_document_cidr(d) for d in documents]
        target = FakeTarget(sections={"Shared": {"id": 3}}, subnets={3: {}})
        actions = build_plan(documents, target, config())

        created = [a.cidr for a in actions if a.kind == "create_subnet"]
        self.assertEqual(len(created), 4)
        # Parent before child, as the nesting requires.
        self.assertLess(created.index("10.20.0.0/16"),
                        created.index("10.20.5.0/24"))
        self.assertEqual(
            len([a for a in actions if a.kind == "create_address"]), 5)


if __name__ == "__main__":
    unittest.main()
