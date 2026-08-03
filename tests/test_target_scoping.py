"""
The real TargetView's ownership rules, exercised against a fake client.

These exist because tests/test_plan.py drives build_plan through a
FakeTarget that reimplements these lookups. That proves the *planner*
does the right thing with the answers it is given, and proves nothing
whatsoever about the answers TargetView actually gives -- a mutation that
deleted the ownership check in TargetView left the whole plan suite
passing.

What is under test here is the rule that keeps a fan-in master safe:
only objects scoped to ONE section may ever be deletion candidates. An
L2 domain can serve several sections, and domain 1 ("default") serves
every section implicitly, so anything looser would let one subordinate
delete another subordinate's VLANs.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync.target import TargetView  # noqa: E402
from test_end_to_end import FakePhpIpam  # noqa: E402


class TestDomainOwnership(unittest.TestCase):
    def setUp(self):
        self.client = FakePhpIpam()
        # Domain 1 exists in every phpIPAM install and is implicitly in
        # every section; the fake seeds it already.
        self.client.add_l2domain("Site-A", sections=["3"])
        self.client.add_l2domain("Site-B", sections=["7"])
        self.client.add_l2domain("Shared-Dom", sections=["3", "7"])
        self.view = TargetView(self.client)

    def _names(self, section_id):
        return sorted(d["name"] for d in self.view.domains_owned_by(section_id))

    def test_only_domains_scoped_to_exactly_this_section_are_owned(self):
        self.assertEqual(self._names(3), ["Site-A"])
        self.assertEqual(self._names(7), ["Site-B"])

    def test_the_default_domain_is_never_owned(self):
        """It has no section list and belongs to everyone, so a VLAN in
        it may belong to any subordinate."""
        self.assertNotIn("default", self._names(3))
        self.assertNotIn("default", self._names(7))

    def test_a_domain_shared_between_sections_is_owned_by_neither(self):
        self.assertNotIn("Shared-Dom", self._names(3))
        self.assertNotIn("Shared-Dom", self._names(7))

    def test_a_section_with_no_domain_of_its_own_owns_nothing(self):
        self.assertEqual(self._names(99), [])


class TestVrfOwnership(unittest.TestCase):
    def setUp(self):
        self.client = FakePhpIpam()
        self.client.add_vrf("MINE", sections=["3"])
        self.client.add_vrf("THEIRS", sections=["7"])
        self.client.add_vrf("SHARED", sections=["3", "7"])
        self.view = TargetView(self.client)

    def _names(self, section_id):
        return sorted(v["name"] for v in self.view.vrfs_owned_by(section_id))

    def test_only_vrfs_scoped_to_exactly_this_section_are_owned(self):
        self.assertEqual(self._names(3), ["MINE"])

    def test_a_vrf_shared_with_another_section_is_owned_by_neither(self):
        """Deleting it would remove it from the other section too."""
        self.assertNotIn("SHARED", self._names(3))
        self.assertNotIn("SHARED", self._names(7))


class TestVrfLookupIsSectionScoped(unittest.TestCase):
    def test_the_same_vrf_name_in_two_sections_is_two_records(self):
        client = FakePhpIpam()
        client.add_vrf("CORE", sections=["3"], rd="65000:1")
        client.add_vrf("CORE", sections=["7"], rd="65000:2")
        view = TargetView(client)
        self.assertEqual(view.vrf_by_name("CORE", 3)["rd"], "65000:1")
        self.assertEqual(view.vrf_by_name("CORE", 7)["rd"], "65000:2")
        self.assertIsNone(view.vrf_by_name("CORE", 99))


class TestFolderEmptinessCheck(unittest.TestCase):
    """phpIPAM deletes a folder's entire contents along with it, so the
    executor confirms emptiness against the live instance first."""

    def test_subnets_under_sees_children_of_a_folder(self):
        client = FakePhpIpam()
        section = client.add_section("Shared")
        folder = client.add_folder(section, "Datacentre")
        client.add_subnet(section, "10.20.5.0", 24, master_subnet_id=folder)
        client.add_folder(section, "Elsewhere")
        view = TargetView(client)
        children = view.subnets_under(section, folder)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["subnet"], "10.20.5.0")

    def test_an_empty_folder_reports_no_children(self):
        client = FakePhpIpam()
        section = client.add_section("Shared")
        folder = client.add_folder(section, "Spare")
        view = TargetView(client)
        self.assertEqual(view.subnets_under(section, folder), [])


if __name__ == "__main__":
    unittest.main()
