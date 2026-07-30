"""
Tests for the empty-collection handling in ipamsync.client_ext.

These are regression tests for a bug found only by running against a real
phpIPAM (1.7, docker `phpipam/phpipam-www:latest`, 2026-07-23): phpIPAM
returns **404 with a "No ... found" message** for an empty collection,
rather than 200 with an empty list.

    GET subnets/11/addresses/   -> 404 "No addresses found"
    GET sections/500/subnets/   -> 404 "No subnets found"

Unhandled, that broke two entirely ordinary cases: exporting a supernet
(which has no addresses of its own), and the *first* import into a target
section (which is by definition empty). No amount of testing against a
fake caught it, because the fake returned what the API documentation
implies rather than what the software does.

The distinction that matters here is empty vs missing: an empty
collection is normal, but a 404 for a subnet that does not exist must
still raise, or the importer would happily write records into a subnet
that was never there.
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
        # Verbatim from the live instance.
        for message in ("No addresses found", "No subnets found",
                        "No sections found"):
            error = PhpIpamError(
                f"GET x failed: {message}", status_code=404, api_code=404,
            )
            self.assertTrue(
                SyncPhpIpamClient._is_empty_collection(error), message,
            )

    def test_negative_control_a_genuine_missing_object_still_raises(self):
        # The critical distinction. If this ever returns True, the
        # importer would treat "that subnet does not exist" as "that
        # subnet is empty" and write records against a bogus id.
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
        # A 401 returning [] would make a misconfigured token look like
        # "the source section is empty" and quietly mirror nothing.
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
