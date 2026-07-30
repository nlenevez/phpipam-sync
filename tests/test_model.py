"""
Tests for the field policy.

Note the deliberate pairing throughout this file: every "these two
records compare equal" assertion is followed by a negative control
proving the comparison can still report a difference. Without that, a
bug that made diff_fields() always return {} would pass the happy-path
tests and silently stop replicating changes forever.
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
        # If everything landed in `dropped`, the schema-drift report
        # would be noise and nobody would read it.
        _, dropped = model.partition_subnet(self.raw)
        for known in ("id", "vlanId", "editDate", "description", "custom_Owner"):
            self.assertNotIn(known, dropped)


class TestPartitionSubnetAgainstLiveSchema(unittest.TestCase):
    """The subnet field policy checked against a record captured verbatim
    from a real phpIPAM 1.7 API (docker phpipam/phpipam-www, 2026-07-23).

    Testing the policy against a hand-written record only proves it is
    self-consistent. This proves it covers what the software actually
    emits -- which is where `gatewayId` turned up: an id pointing into
    this instance's own ipaddresses table that no documentation-derived
    list had accounted for.
    """

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
        # No field from a real instance should land in the drift report:
        # each is either carried or deliberately excluded. A failure here
        # means phpIPAM grew a field the policy has not been told about.
        _, dropped = model.partition_subnet(self.LIVE_SUBNET)
        self.assertEqual(dropped, [])

    def test_gateway_id_is_never_carried(self):
        # gatewayId indexes the *ipaddresses* table of the source
        # instance. Copied across, it would name an unrelated address as
        # the target subnet's gateway.
        carried, _ = model.partition_subnet(self.LIVE_SUBNET)
        self.assertNotIn("gatewayId", carried)
        self.assertNotIn("gateway", carried)

    def test_gateway_still_survives_replication_via_the_address_flag(self):
        # Dropping gatewayId is only safe because the gateway is also
        # expressed as is_gateway on the address itself, which IS carried.
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
        # Proves test_every_live_field_is_accounted_for is not passing
        # merely because the drift report is broken.
        record = dict(self.LIVE_SUBNET, brandNewInPhpipam9="x")
        _, dropped = model.partition_subnet(record)
        self.assertEqual(dropped, ["brandNewInPhpipam9"])


class TestCustomFields(unittest.TestCase):
    """Custom-field handling, verified against phpIPAM 1.8.1.

    phpIPAM decides what is custom by diffing the live table against its
    shipped SCHEMA.sql, so a custom field's name is whatever the admin
    typed -- a field added through the UI as "Owner" is a column named
    `Owner`, with no prefix. The only reliable signal is phpIPAM's own
    nested `custom_fields` block, emitted when the API app has "nest
    custom fields" enabled.
    """

    NESTED = {
        "id": 12, "description": "Shared /24",
        "custom_fields": {"Owner": "netops-team", "custom_Notes": "n"},
    }

    def test_nested_custom_fields_are_carried_flat(self):
        # Read nested, store flat -- writes must be flat, because posting
        # a nested object is rejected outright.
        carried, _ = model.partition_subnet(self.NESTED)
        self.assertEqual(carried["Owner"], "netops-team")
        self.assertEqual(carried["custom_Notes"], "n")

    def test_the_container_key_is_never_carried_as_a_field(self):
        # Regression: "custom_fields" itself starts with "custom_", so
        # the prefix fallback used to carry the whole dict as a field.
        # phpIPAM then rejects the write with
        # "Invalid request key custom_fields" and the subnet is never
        # created -- which took every address in it down too.
        carried, dropped = model.partition_subnet(self.NESTED)
        self.assertNotIn("custom_fields", carried)
        self.assertNotIn("custom_fields", dropped)

    def test_unprefixed_custom_field_without_nesting_is_reported_not_carried(self):
        # With nesting off, phpIPAM returns custom fields flat and
        # indistinguishable from its own. Carrying unknown fields blindly
        # is exactly the mistake the allowlist prevents, so they are
        # reported instead -- that report is what tells you to turn
        # nesting on.
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
    """Regression tests for an endless-update loop found on 1.8.1.

    The snapshot stores custom fields flat, but a target record read back
    with nesting enabled has them under `custom_fields`. Comparing the
    two without flattening looks for a flat `Owner`, never finds it, and
    rewrites every custom field on every run forever -- churning the
    target's history and burying real changes.
    """

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
        # The actual bug: this diff must be empty, or the importer
        # rewrites the custom fields on every single run.
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
    """Regression tests for phpIPAM's read/write field-name asymmetry.

    Found only by applying against a real instance: `POST subnets/` with
    the field name the API itself returns on read is rejected outright
    with 400 "Invalid request key tag". Every subnet create and update
    failed until this was mapped.
    """

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
        # The aliasing must happen at write time only -- the snapshot
        # stores what the API reports, so the file format does not encode
        # one instance's write quirk.
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
