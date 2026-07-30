"""
Tests for the import planner.

The planner is the part of this tool that decides what happens to the
target instance, so it is tested against a fake target rather than a
live one. The single most important assertion in this file is
test_default_plan_never_deletes: by default a bad snapshot can only add
or update, never destroy. Deletion exists but is opt-in, and the tests
in TestDeleteSafetyLimit guard the case that actually matters -- an empty
or mis-scoped snapshot arriving over a link the replica cannot question.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipamsync.plan import PlanError, build_plan, order_documents, summarise  # noqa: E402
from ipamsync.snapshot import build_subnet_document  # noqa: E402


class FakeTarget:
    """Stands in for TargetView. Same three methods build_plan() uses."""

    def __init__(self, sections=None, subnets=None, addresses=None):
        self._sections = sections or {}          # name -> {"id": n}
        self._subnets = subnets or {}            # section id -> {cidr: raw}
        self._addresses = addresses or {}        # subnet id -> {ip: raw}

    def section_by_name(self, name):
        for existing, value in self._sections.items():
            if existing.casefold() == str(name).casefold():
                return value
        return None

    def subnets_by_cidr(self, section_id):
        return self._subnets.get(section_id, {})

    def addresses_by_ip(self, subnet_id):
        return self._addresses.get(subnet_id, {})


def config(section_map=None, **options):
    base = {"sync_tags": True, "create_missing_sections": False,
            "delete_drift": False, "delete_limit_fraction": 0.1,
            "force_delete": False}
    base.update(options)
    return {"options": base, "section_map": section_map or {}}


def document(cidr="10.20.0.0/24", section="Shared", fields=None,
             addresses=None, master_subnet=None, vlan=None):
    return build_subnet_document(
        section_name=section,
        cidr=cidr,
        fields=fields if fields is not None else {"description": "core"},
        addresses=addresses or [],
        master_subnet=master_subnet,
        vlan=vlan,
    )


def kinds(actions):
    return [action.kind for action in actions]


class TestSectionResolution(unittest.TestCase):
    def test_missing_section_is_a_hard_failure_by_default(self):
        with self.assertRaises(PlanError) as ctx:
            build_plan([document()], FakeTarget(), config())
        self.assertIn("has no section", str(ctx.exception))

    def test_missing_section_can_be_created_when_enabled(self):
        actions = build_plan(
            [document()], FakeTarget(), config(create_missing_sections=True),
        )
        self.assertEqual(actions[0].kind, "create_section")

    def test_section_map_redirects_to_a_differently_named_target_section(self):
        target = FakeTarget(sections={"Shared-from-1": {"id": 9}}, subnets={9: {}})
        actions = build_plan(
            [document()], target, config(section_map={"Shared": "Shared-from-1"}),
        )
        self.assertEqual(kinds(actions), ["create_subnet"])

    def test_section_match_is_case_insensitive(self):
        target = FakeTarget(sections={"shared": {"id": 9}}, subnets={9: {}})
        actions = build_plan([document()], target, config())
        self.assertEqual(kinds(actions), ["create_subnet"])


class TestSubnetActions(unittest.TestCase):
    def setUp(self):
        self.sections = {"Shared": {"id": 3}}

    def test_absent_subnet_is_created(self):
        target = FakeTarget(sections=self.sections, subnets={3: {}})
        actions = build_plan([document()], target, config())
        self.assertEqual(kinds(actions), ["create_subnet"])

    def test_differing_subnet_is_updated_with_only_the_changed_fields(self):
        target = FakeTarget(
            sections=self.sections,
            subnets={3: {"10.20.0.0/24": {
                "id": 42, "description": "stale", "showName": "1",
            }}},
            addresses={42: {}},
        )
        actions = build_plan(
            [document(fields={"description": "core", "showName": "1"})],
            target, config(),
        )
        self.assertEqual(kinds(actions), ["update_subnet"])
        self.assertEqual(actions[0].detail, {"description": "core"})

    def test_matching_subnet_produces_no_action_at_all(self):
        target = FakeTarget(
            sections=self.sections,
            subnets={3: {"10.20.0.0/24": {"id": 42, "description": "core"}}},
            addresses={42: {}},
        )
        actions = build_plan([document()], target, config())
        self.assertEqual(actions, [])

    def test_vlan_on_source_produces_a_visible_note(self):
        target = FakeTarget(
            sections=self.sections,
            subnets={3: {"10.20.0.0/24": {"id": 42, "description": "core"}}},
            addresses={42: {}},
        )
        actions = build_plan(
            [document(vlan={"number": "100", "name": "shared"})], target, config(),
        )
        self.assertEqual(kinds(actions), ["note"])
        self.assertFalse(actions[0].is_write)


class TestAddressActions(unittest.TestCase):
    def setUp(self):
        self.target = FakeTarget(
            sections={"Shared": {"id": 3}},
            subnets={3: {"10.20.0.0/24": {"id": 42, "description": "core"}}},
            addresses={42: {
                "10.20.0.2": {"id": 900, "hostname": "sw2", "description": "old"},
            }},
        )

    def test_new_address_is_created(self):
        actions = build_plan(
            [document(addresses=[("10.20.0.3", {"hostname": "sw3"})])],
            self.target, config(),
        )
        self.assertIn("create_address", kinds(actions))

    def test_changed_address_is_updated_with_only_changed_fields(self):
        actions = build_plan(
            [document(addresses=[
                ("10.20.0.2", {"hostname": "sw2", "description": "new"}),
            ])],
            self.target, config(),
        )
        updates = [a for a in actions if a.kind == "update_address"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].detail, {"description": "new"})

    def test_unchanged_address_produces_no_write(self):
        actions = build_plan(
            [document(addresses=[
                ("10.20.0.2", {"hostname": "sw2", "description": "old"}),
            ])],
            self.target, config(),
        )
        self.assertEqual([a for a in actions if a.is_write], [])


class TestAdditiveOnly(unittest.TestCase):
    def setUp(self):
        self.target = FakeTarget(
            sections={"Shared": {"id": 3}},
            subnets={
                3: {
                    "10.20.0.0/24": {"id": 42, "description": "core"},
                    "10.99.0.0/24": {"id": 43, "description": "local only"},
                }
            },
            addresses={42: {
                "10.20.0.2": {"id": 900, "hostname": "sw2"},
                "10.20.0.250": {"id": 901, "hostname": "local-only"},
            }},
        )

    def test_target_only_subnet_is_reported_as_drift(self):
        actions = build_plan([document()], self.target, config())
        drift = [a for a in actions if a.kind == "drift_subnet"]
        self.assertEqual([a.key for a in drift], ["10.99.0.0/24"])

    def test_target_only_address_is_reported_as_drift(self):
        actions = build_plan(
            [document(addresses=[("10.20.0.2", {"hostname": "sw2"})])],
            self.target, config(),
        )
        drift = [a for a in actions if a.kind == "drift_address"]
        self.assertEqual([a.ip for a in drift], ["10.20.0.250"])

    def test_drift_actions_are_never_writes(self):
        actions = build_plan([document()], self.target, config())
        for action in actions:
            if action.kind.startswith("drift_"):
                self.assertFalse(action.is_write, f"{action} must not be executed")

    def test_default_plan_never_deletes(self):
        # The core safety promise: with deletion off -- the default --
        # nothing in a plan can destroy data, whatever the snapshot says.
        actions = build_plan(
            [document(addresses=[("10.20.0.3", {"hostname": "sw3"})])],
            self.target, config(),
        )
        for action in actions:
            self.assertNotIn("delete", action.kind)
            self.assertNotIn("remove", action.kind)

    def test_deletion_requires_the_option(self):
        # An explicit check that the option is the ONLY thing standing
        # between a drift report and a destructive action, so it cannot
        # be switched on as a side effect of anything else.
        off = build_plan([document()], self.target, config())
        on = build_plan([document()], self.target, config(delete_drift=True))
        self.assertTrue(any(a.kind == "drift_subnet" for a in off))
        self.assertFalse(any(a.kind.startswith("delete_") for a in off))
        self.assertTrue(any(a.kind == "delete_subnet" for a in on))
        self.assertFalse(any(a.kind.startswith("drift_") for a in on))


class TestDeletion(unittest.TestCase):
    """Strict-mirror mode. Deletion is the only operation here that can
    destroy data, so the ordering and the safety limit matter more than
    the happy path."""

    def _target(self, extra_subnets=None, extra_addresses=None):
        subnets = {"10.20.0.0/24": {"id": 42, "description": "core"}}
        subnets.update(extra_subnets or {})
        addresses = {42: {"10.20.0.2": {"id": 900, "hostname": "sw2"}}}
        addresses.update(extra_addresses or {})
        return FakeTarget(sections={"Shared": {"id": 3}},
                          subnets={3: subnets}, addresses=addresses)

    def test_orphan_address_becomes_a_delete(self):
        target = self._target(extra_addresses={42: {
            "10.20.0.2": {"id": 900, "hostname": "sw2"},
            "10.20.0.99": {"id": 901, "hostname": "gone-upstream"},
        }})
        actions = build_plan(
            [document(addresses=[("10.20.0.2", {"hostname": "sw2"})])],
            target, config(delete_drift=True),
        )
        deletes = [a for a in actions if a.kind == "delete_address"]
        self.assertEqual([a.ip for a in deletes], ["10.20.0.99"])
        self.assertTrue(deletes[0].is_write)

    def test_orphan_subnet_becomes_a_delete(self):
        target = self._target(
            extra_subnets={"10.99.0.0/24": {"id": 43, "description": "gone"}})
        actions = build_plan([document()], target, config(delete_drift=True))
        self.assertEqual([a.cidr for a in actions if a.kind == "delete_subnet"],
                         ["10.99.0.0/24"])

    def test_addresses_are_deleted_before_subnets(self):
        # Otherwise a subnet could go while addresses still reference it.
        target = self._target(
            extra_subnets={"10.99.0.0/24": {"id": 43, "description": "gone"}},
            extra_addresses={42: {
                "10.20.0.2": {"id": 900, "hostname": "sw2"},
                "10.20.0.99": {"id": 901, "hostname": "gone"},
            }})
        actions = build_plan(
            [document(addresses=[("10.20.0.2", {"hostname": "sw2"})])],
            target, config(delete_drift=True),
        )
        kinds = [a.kind for a in actions if a.kind.startswith("delete_")]
        self.assertLess(kinds.index("delete_address"), kinds.index("delete_subnet"))

    def test_child_subnets_are_deleted_before_parents(self):
        target = self._target(extra_subnets={
            "10.99.0.0/16": {"id": 43, "description": "parent"},
            "10.99.5.0/24": {"id": 44, "description": "child"},
        })
        actions = build_plan([document()], target, config(delete_drift=True))
        order = [a.cidr for a in actions if a.kind == "delete_subnet"]
        self.assertLess(order.index("10.99.5.0/24"), order.index("10.99.0.0/16"))

    def test_addresses_inside_a_doomed_subnet_are_not_listed_separately(self):
        # They vanish with the subnet; listing them would double-count
        # against the safety limit and make pointless API calls.
        target = self._target(
            extra_subnets={"10.99.0.0/24": {"id": 43, "description": "gone"}},
            extra_addresses={43: {"10.99.0.5": {"id": 950, "hostname": "x"}}})
        # Keep the live subnet's own address so the only candidate left
        # is the one inside the doomed subnet.
        actions = build_plan(
            [document(addresses=[("10.20.0.2", {"hostname": "sw2"})])],
            target, config(delete_drift=True))
        self.assertEqual(
            [a.ip for a in actions if a.kind == "delete_address"], [],
            "addresses inside a subnet being deleted must not be listed too")
        self.assertEqual(
            [a.cidr for a in actions if a.kind == "delete_subnet"],
            ["10.99.0.0/24"])


class TestDeleteSafetyLimit(unittest.TestCase):
    """The guard that matters. An empty or mis-scoped snapshot arriving
    over a one-way link must not be able to wipe the replica."""

    def _target_with(self, count):
        subnets = {"10.20.0.0/24": {"id": 42, "description": "core"}}
        addresses = {42: {
            f"10.20.0.{n}": {"id": 900 + n, "hostname": f"h{n}"}
            for n in range(1, count + 1)
        }}
        return FakeTarget(sections={"Shared": {"id": 3}},
                          subnets={3: subnets}, addresses=addresses)

    def test_a_mass_deletion_is_refused(self):
        target = self._target_with(100)
        with self.assertRaises(PlanError) as ctx:
            build_plan([document(addresses=[])], target,
                       config(delete_drift=True))
        message = str(ctx.exception)
        self.assertIn("safety limit", message)
        # The message must point at the likely cause, not just the number.
        self.assertIn("snapshot", message)

    def test_a_small_deletion_is_allowed(self):
        target = self._target_with(100)
        keep = [(f"10.20.0.{n}", {"hostname": f"h{n}"}) for n in range(1, 96)]
        actions = build_plan([document(addresses=keep)], target,
                             config(delete_drift=True))
        self.assertEqual(
            len([a for a in actions if a.kind == "delete_address"]), 5)

    def test_force_delete_overrides_the_limit(self):
        target = self._target_with(100)
        actions = build_plan([document(addresses=[])], target,
                             config(delete_drift=True, force_delete=True))
        self.assertEqual(
            len([a for a in actions if a.kind == "delete_address"]), 100)

    def test_small_datasets_are_not_blocked_by_the_floor(self):
        # 10% of 4 records rounds to 0; the floor keeps a tiny dataset
        # from being permanently unable to delete anything.
        target = self._target_with(3)
        actions = build_plan([document(addresses=[])], target,
                             config(delete_drift=True))
        self.assertEqual(
            len([a for a in actions if a.kind == "delete_address"]), 3)

    def test_the_limit_is_configurable(self):
        target = self._target_with(100)
        keep = [(f"10.20.0.{n}", {"hostname": f"h{n}"}) for n in range(1, 71)]
# 30 deletions: over the default 10% of 101, under an explicit 50%.
        with self.assertRaises(PlanError):
            build_plan([document(addresses=keep)], target,
                       config(delete_drift=True))
        actions = build_plan([document(addresses=keep)], target,
                             config(delete_drift=True,
                                    delete_limit_fraction=0.5))
        self.assertEqual(
            len([a for a in actions if a.kind == "delete_address"]), 30)

    def test_limit_does_not_apply_when_deletion_is_off(self):
        # Additive runs report unlimited drift without complaint.
        target = self._target_with(100)
        actions = build_plan([document(addresses=[])], target, config())
        self.assertEqual(
            len([a for a in actions if a.kind == "drift_address"]), 100)


class TestNestingOrder(unittest.TestCase):
    def test_parent_is_ordered_before_child(self):
        child = document(cidr="10.20.0.0/24", master_subnet="10.20.0.0/16")
        parent = document(cidr="10.20.0.0/16")
        ordered = [d["cidr"] for d in order_documents([child, parent])]
        self.assertLess(ordered.index("10.20.0.0/16"), ordered.index("10.20.0.0/24"))

    def test_creates_follow_nesting_order(self):
        target = FakeTarget(sections={"Shared": {"id": 3}}, subnets={3: {}})
        actions = build_plan(
            [document(cidr="10.20.0.0/24", master_subnet="10.20.0.0/16"),
             document(cidr="10.20.0.0/16")],
            target, config(),
        )
        created = [a.cidr for a in actions if a.kind == "create_subnet"]
        self.assertEqual(created, ["10.20.0.0/16", "10.20.0.0/24"])

    def test_orphaned_parent_reference_does_not_lose_the_subnet(self):
        # Parent outside the replicated scope: the child must still be
        # planned (created top-level), not silently dropped.
        target = FakeTarget(sections={"Shared": {"id": 3}}, subnets={3: {}})
        actions = build_plan(
            [document(cidr="10.20.0.0/24", master_subnet="10.0.0.0/8")],
            target, config(),
        )
        self.assertEqual([a.cidr for a in actions if a.kind == "create_subnet"],
                         ["10.20.0.0/24"])

    def test_cycle_does_not_hang_or_lose_documents(self):
        a = document(cidr="10.20.0.0/24", master_subnet="10.20.1.0/24")
        b = document(cidr="10.20.1.0/24", master_subnet="10.20.0.0/24")
        ordered = order_documents([a, b])
        self.assertEqual(len(ordered), 2)


class TestSummarise(unittest.TestCase):
    def test_counts_by_kind(self):
        target = FakeTarget(sections={"Shared": {"id": 3}}, subnets={3: {}})
        actions = build_plan(
            [document(addresses=[("10.20.0.2", {"hostname": "sw2"}),
                                 ("10.20.0.3", {"hostname": "sw3"})])],
            target, config(),
        )
        self.assertEqual(
            summarise(actions), {"create_subnet": 1, "create_address": 2},
        )


if __name__ == "__main__":
    unittest.main()
