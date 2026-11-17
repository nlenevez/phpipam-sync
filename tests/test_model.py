"""
Tests for the field policy.

Each "compares equal" assertion is paired with a negative control proving
the comparison can still report a difference.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipamsync import model  # noqa: E402


class TestNormalise(unittest.TestCase):
    def test_unset_forms_collapse_to_the_same_value(self):
        self.assertEqual(model.normalise(None), model.normalise(""))

    def test_loose_types_from_the_api_compare_equal(self):
        # phpIPAM returns the same logical field as int, str, or bool
        # depending on version and endpoint.
        self.assertEqual(model.normalise(1), model.normalise("1"))
        self.assertEqual(model.normalise(True), model.normalise("1"))
        self.assertEqual(model.normalise(False), model.normalise("0"))

    def test_negative_control_distinct_values_stay_distinct(self):
        # If normalise() over-collapsed, every diff would vanish.
        self.assertNotEqual(model.normalise("0"), model.normalise(""))
        self.assertNotEqual(model.normalise("1"), model.normalise("2"))
        self.assertNotEqual(model.normalise("host-a"), model.normalise("host-b"))


class TestDiffFields(unittest.TestCase):
    def test_identical_records_produce_no_writes(self):
        desired = {"description": "core", "showName": "1"}
        current = {"description": "core", "showName": 1, "id": "42"}
        self.assertEqual(model.diff_fields(desired, current), {})

    def test_negative_control_a_real_change_is_detected(self):
        desired = {"description": "core", "showName": "1"}
        current = {"description": "edge", "showName": 1}
        self.assertEqual(model.diff_fields(desired, current), {"description": "core"})

    def test_missing_field_on_target_counts_as_a_change(self):
        self.assertEqual(
            model.diff_fields({"hostname": "sw1"}, {}), {"hostname": "sw1"},
        )

    def test_setting_a_field_to_empty_is_detected(self):
        # Clearing a description on the source must propagate, so ""
        # against a populated target is a real change.
        self.assertEqual(
            model.diff_fields({"description": ""}, {"description": "old"}),
            {"description": ""},
        )

    def test_extra_fields_on_target_are_not_reported(self):
        # The snapshot describes what the source knows; it does not
        # assert that everything else on the target is empty.
        self.assertEqual(
            model.diff_fields({"hostname": "sw1"},
                              {"hostname": "sw1", "note": "local note"}),
            {},
        )


class TestPartitionSubnet(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "id": "42",
            "subnet": "10.20.0.0",
            "mask": "24",
            "sectionId": "3",
            "description": "Shared core",
            "showName": "1",
            "vlanId": "17",
            "nameserverId": "2",
            "editDate": "2026-07-20 10:00:00",
            "custom_Owner": "netops",
            "somethingNewInPhpipam9": "x",
        }

    def test_literal_fields_are_carried(self):
        carried, _ = model.partition_subnet(self.raw)
        self.assertEqual(carried["description"], "Shared core")
        self.assertEqual(carried["showName"], "1")

    def test_local_foreign_keys_are_never_carried(self):
        # The core safety property: copying these across instances would
        # point the record at unrelated rows on the target.
        carried, _ = model.partition_subnet(self.raw)
        for dangerous in ("id", "sectionId", "vlanId", "nameserverId"):
            self.assertNotIn(dangerous, carried)

    def test_derived_state_is_not_carried(self):
        carried, _ = model.partition_subnet(self.raw)
        self.assertNotIn("editDate", carried)

    def test_custom_fields_pass_through(self):
        carried, _ = model.partition_subnet(self.raw)
        self.assertEqual(carried["custom_Owner"], "netops")

    def test_unknown_fields_are_reported_not_silently_dropped(self):
        _, dropped = model.partition_subnet(self.raw)
        self.assertIn("somethingNewInPhpipam9", dropped)

    def test_negative_control_known_fields_do_not_pollute_the_report(self):
        # Only genuinely unknown fields belong in `dropped`.
        _, dropped = model.partition_subnet(self.raw)
        for known in ("id", "vlanId", "editDate", "description", "custom_Owner"):
            self.assertNotIn(known, dropped)


class TestPartitionSubnetAgainstLiveSchema(unittest.TestCase):
    """The subnet field policy checked against a record captured from a
    real phpIPAM 1.7 API."""

    LIVE_SUBNET = {
        "DNSrecords": 0, "DNSrecursive": 0, "allowRequests": 0,
        "calculation": {"Type": "IPv4"}, "customer_id": None,
        "description": "Shared /24", "device": 0, "discoverSubnet": 0,
        "editDate": None, "firewallAddressObject": None,
        "gateway": {"ip_addr": "10.20.5.2", "id": 101}, "gatewayId": 101,
        "id": 12, "isFolder": 0, "isFull": 0, "isPool": 0,
        "lastDiscovery": None, "lastScan": None, "linked_subnet": None,
        "location": None, "mask": "24", "masterSubnetId": 11,
        "nameserverId": 0, "permissions": None, "pingSubnet": 1,
        "resolveDNS": 0, "scanAgent": None, "sectionId": 1, "showName": 0,
        "subnet": "10.20.5.0", "tag": 2, "threshold": 0, "vlanId": 10,
        "vrfId": None,
    }

    def test_every_live_field_is_accounted_for(self):
        # Every real field is either carried or deliberately excluded; a
        # failure means phpIPAM grew a field the policy does not know.
        _, dropped = model.partition_subnet(self.LIVE_SUBNET)
        self.assertEqual(dropped, [])

    def test_gateway_id_is_never_carried(self):
        # gatewayId is an instance-local ipaddresses id.
        carried, _ = model.partition_subnet(self.LIVE_SUBNET)
        self.assertNotIn("gatewayId", carried)
        self.assertNotIn("gateway", carried)

    def test_gateway_still_survives_replication_via_the_address_flag(self):
        # The gateway is carried as is_gateway on the address instead.
        carried, _ = model.partition_address(
            {"ip": "10.20.5.2", "is_gateway": 1, "hostname": "gw"}
        )
        self.assertEqual(carried["is_gateway"], "1")

    def test_ispool_is_carried(self):
        carried, _ = model.partition_subnet(self.LIVE_SUBNET)
        self.assertIn("isPool", carried)

    def test_subnet_tag_follows_the_same_option_as_address_tag(self):
        carried, _ = model.partition_subnet(self.LIVE_SUBNET)
        self.assertEqual(carried["tag"], "2")
        carried, dropped = model.partition_subnet(
            self.LIVE_SUBNET, {"sync_tags": False}
        )
        self.assertNotIn("tag", carried)
        self.assertNotIn("tag", dropped)

    def test_negative_control_a_genuinely_new_field_is_reported(self):
        # Negative control: proves the drift report is not simply broken.
        record = dict(self.LIVE_SUBNET, brandNewInPhpipam9="x")
        _, dropped = model.partition_subnet(record)
        self.assertEqual(dropped, ["brandNewInPhpipam9"])


class TestCustomFields(unittest.TestCase):
    """Custom-field handling.

    Custom field names are whatever the admin chose, with no prefix. The
    reliable signal is the nested `custom_fields` block, emitted when the
    API app has "Nest custom fields" enabled.
    """

    NESTED = {
        "id": 12, "description": "Shared /24",
        "custom_fields": {"Owner": "netops-team", "custom_Notes": "n"},
    }

    def test_nested_custom_fields_are_carried_flat(self):
        # Read nested, store flat: phpIPAM rejects nested writes.
        carried, _ = model.partition_subnet(self.NESTED)
        self.assertEqual(carried["Owner"], "netops-team")
        self.assertEqual(carried["custom_Notes"], "n")

    def test_the_container_key_is_never_carried_as_a_field(self):
        # "custom_fields" itself starts with "custom_" and must not be
        # carried as a field ("Invalid request key custom_fields").
        carried, dropped = model.partition_subnet(self.NESTED)
        self.assertNotIn("custom_fields", carried)
        self.assertNotIn("custom_fields", dropped)

    def test_unprefixed_custom_field_without_nesting_is_reported_not_carried(self):
        # With nesting off, custom fields are indistinguishable from
        # standard ones, so they are reported, not carried.
        carried, dropped = model.partition_subnet(
            {"id": 12, "description": "d", "Owner": "netops-team"}
        )
        self.assertNotIn("Owner", carried)
        self.assertIn("Owner", dropped)

    def test_prefixed_custom_field_still_works_without_nesting(self):
        carried, _ = model.partition_subnet(
            {"id": 12, "description": "d", "custom_Notes": "n"}
        )
        self.assertEqual(carried["custom_Notes"], "n")

    def test_addresses_handle_nesting_the_same_way(self):
        carried, dropped = model.partition_address(
            {"ip": "10.20.5.2", "hostname": "gw",
             "custom_fields": {"AssetTag": "ASSET-0042"}}
        )
        self.assertEqual(carried["AssetTag"], "ASSET-0042")
        self.assertNotIn("custom_fields", carried)


class TestFlattenCustomFields(unittest.TestCase):
    """Target records read back nested must be flattened before diffing
    against the flat snapshot, or every run rewrites every custom field."""

    def test_nested_block_is_lifted_to_the_top_level(self):
        flat = model.flatten_custom_fields(
            {"id": 12, "description": "d",
             "custom_fields": {"Owner": "netops", "AssetTag": "A1"}}
        )
        self.assertEqual(flat["Owner"], "netops")
        self.assertEqual(flat["AssetTag"], "A1")
        self.assertNotIn("custom_fields", flat)

    def test_standard_fields_survive(self):
        flat = model.flatten_custom_fields(
            {"id": 12, "description": "d", "custom_fields": {"Owner": "x"}}
        )
        self.assertEqual(flat["id"], 12)
        self.assertEqual(flat["description"], "d")

    def test_record_without_nesting_is_unchanged(self):
        raw = {"id": 12, "description": "d"}
        self.assertIs(model.flatten_custom_fields(raw), raw)

    def test_flattened_target_compares_equal_to_the_snapshot(self):
        # Must be empty, or every run rewrites the custom fields.
        snapshot_fields = {"description": "d", "Owner": "netops"}
        target_raw = {"id": 12, "description": "d",
                      "custom_fields": {"Owner": "netops"}}
        self.assertEqual(
            model.diff_fields(snapshot_fields,
                              model.flatten_custom_fields(target_raw)),
            {},
        )

    def test_negative_control_a_real_custom_field_change_is_still_detected(self):
        snapshot_fields = {"Owner": "netops"}
        target_raw = {"custom_fields": {"Owner": "someone-else"}}
        self.assertEqual(
            model.diff_fields(snapshot_fields,
                              model.flatten_custom_fields(target_raw)),
            {"Owner": "netops"},
        )


class TestSubnetWriteAliases(unittest.TestCase):
    """Subnets read as `tag` but written as `state`: `POST subnets/` with
    `tag` is 400 "Invalid request key tag"."""

    def test_tag_is_renamed_to_state_for_writing(self):
        self.assertEqual(
            model.to_subnet_write_fields({"tag": "2", "description": "x"}),
            {"state": "2", "description": "x"},
        )

    def test_other_fields_are_untouched(self):
        fields = {
            "description": "core", "showName": "1", "isPool": "0",
            "custom_Owner": "netops",
        }
        self.assertEqual(model.to_subnet_write_fields(fields), fields)

    def test_negative_control_the_read_name_is_gone_after_translation(self):
        # If `tag` survived translation, phpIPAM would reject the write.
        written = model.to_subnet_write_fields({"tag": "2"})
        self.assertNotIn("tag", written)

    def test_snapshot_keeps_the_read_name(self):
        # Aliasing happens at write time only; the snapshot stores the
        # read name.
        carried, _ = model.partition_subnet({"tag": 2, "description": "x"})
        self.assertIn("tag", carried)
        self.assertNotIn("state", carried)


class TestPartitionAddressAgainstLiveSchema(unittest.TestCase):
    """The address field policy against a live phpIPAM 1.7 record."""

    LIVE_ADDRESS = {
        "PTR": 0, "PTRignore": 0, "customer_id": None,
        "description": "gateway router", "deviceId": None, "editDate": None,
        "excludePing": 0, "firewallAddressObject": None, "hostname": "gw-core",
        "id": 101, "ip": "10.20.5.2", "is_gateway": 1,
        "lastSeen": "1970-01-01 00:00:01", "location": None,
        "mac": "aa:bb:cc:dd:ee:02", "note": None, "owner": "netops",
        "port": "Gi0/0", "subnetId": 12, "tag": 2,
    }

    def test_every_live_field_is_accounted_for(self):
        _, dropped = model.partition_address(self.LIVE_ADDRESS)
        self.assertEqual(dropped, [])

    def test_local_ids_are_not_carried(self):
        carried, _ = model.partition_address(self.LIVE_ADDRESS)
        for dangerous in ("id", "subnetId", "deviceId", "PTR", "location"):
            self.assertNotIn(dangerous, carried)


class TestPartitionAddress(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "id": "900",
            "subnetId": "42",
            "ip": "10.20.0.5",
            "hostname": "sw1",
            "description": "access switch",
            "mac": "aa:bb:cc:dd:ee:ff",
            "tag": "2",
            "deviceId": "7",
            "lastSeen": "2026-07-22 09:00:00",
        }

    def test_literal_fields_are_carried(self):
        carried, _ = model.partition_address(self.raw)
        self.assertEqual(carried["hostname"], "sw1")
        self.assertEqual(carried["mac"], "aa:bb:cc:dd:ee:ff")

    def test_local_foreign_keys_and_state_are_not_carried(self):
        carried, _ = model.partition_address(self.raw)
        for dangerous in ("id", "subnetId", "deviceId", "lastSeen"):
            self.assertNotIn(dangerous, carried)

    def test_tag_is_carried_by_default(self):
        carried, _ = model.partition_address(self.raw)
        self.assertEqual(carried["tag"], "2")

    def test_tag_can_be_suppressed_by_option(self):
        carried, dropped = model.partition_address(
            self.raw, {"sync_tags": False}
        )
        self.assertNotIn("tag", carried)
        # Suppressed by choice, so it must not appear as schema drift.
        self.assertNotIn("tag", dropped)


if __name__ == "__main__":
    unittest.main()
