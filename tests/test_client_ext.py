"""
Empty-collection handling in ipamsync.client_ext.

phpIPAM returns 404 "No ... found" for an empty collection, not 200 with
an empty list:

    GET subnets/11/addresses/   -> 404 "No addresses found"
    GET sections/500/subnets/   -> 404 "No subnets found"

Empty must become [], but a 404 for a missing object must still raise.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipamsync.client_ext import SyncPhpIpamClient  # noqa: E402
from phpipam_client import PhpIpamError  # noqa: E402


def client():
    return SyncPhpIpamClient(base_url="https://ipam.test", app_id="sync", token="x")


class TestIsEmptyCollection(unittest.TestCase):
    def test_real_phpipam_empty_messages_are_recognised(self):
        # Messages as phpIPAM returns them.
        for message in ("No addresses found", "No subnets found",
                        "No sections found"):
            error = PhpIpamError(
                f"GET x failed: {message}", status_code=404, api_code=404,
            )
            self.assertTrue(
                SyncPhpIpamClient._is_empty_collection(error), message,
            )

    def test_negative_control_a_genuine_missing_object_still_raises(self):
        # Missing must not be mistaken for empty, or records would be
        # written against a bogus id.
        for message in ("Subnet not found", "Invalid subnet id",
                        "Section does not exist"):
            error = PhpIpamError(
                f"GET x failed: {message}", status_code=404, api_code=404,
            )
            self.assertFalse(
                SyncPhpIpamClient._is_empty_collection(error), message,
            )

    def test_non_404_errors_are_never_treated_as_empty(self):
        for status in (401, 403, 500, 503):
            error = PhpIpamError("no addresses found", status_code=status)
            self.assertFalse(SyncPhpIpamClient._is_empty_collection(error))


class TestReadCollection(unittest.TestCase):
    def test_empty_collection_becomes_an_empty_list(self):
        def raiser():
            raise PhpIpamError(
                "GET subnets/11/addresses/ failed: No addresses found",
                status_code=404,
            )
        self.assertEqual(client()._read_collection(raiser), [])

    def test_other_errors_propagate(self):
        def raiser():
            raise PhpIpamError("GET x failed: Subnet not found", status_code=404)
        with self.assertRaises(PhpIpamError):
            client()._read_collection(raiser)

    def test_auth_failure_is_not_swallowed(self):
        # A 401 returning [] would make a bad token look like an empty section.
        def raiser():
            raise PhpIpamError("401 Unauthorized", status_code=401)
        with self.assertRaises(PhpIpamError):
            client()._read_collection(raiser)

    def test_successful_read_passes_through(self):
        self.assertEqual(
            client()._read_collection(lambda: [{"id": 1}]), [{"id": 1}],
        )

    def test_none_becomes_empty_list(self):
        self.assertEqual(client()._read_collection(lambda: None), [])


if __name__ == "__main__":
    unittest.main()
