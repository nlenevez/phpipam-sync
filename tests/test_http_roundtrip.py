"""
Full-stack test: the real client, over real HTTP, against a local server
that speaks phpIPAM's wire format.

Covers what tests/test_end_to_end.py bypasses: URL construction, the
`token` header, JSON encoding, and unwrapping the `{"success", "data"}`
envelope. The server answers as phpIPAM's documentation describes, so it
cannot catch a case where the documentation is wrong.
"""

import json
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync.client_ext import SyncPhpIpamClient  # noqa: E402
from ipamsync.plan import build_plan, canonicalise_document_cidr  # noqa: E402
from ipamsync.snapshot import read_snapshot  # noqa: E402
from ipamsync.target import Executor, TargetView  # noqa: E402
from test_end_to_end import (  # noqa: E402
    FakePhpIpam, make_config, populated_source, run_export,
)

APP_ID = "sync"
TOKEN = "test-app-code"


def make_handler(store, seen_headers):
    """Build a request handler backed by an in-memory FakePhpIpam,
    answering in phpIPAM's {"code", "success", "data"} envelope."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep the test output clean

        # -- helpers ---------------------------------------------------

        def _send(self, data, status=200, success=True):
            envelope = {"code": status, "success": success, "data": data}
            if not success:
                # phpIPAM puts the error reason in `message`.
                envelope["message"] = data
            body = json.dumps(envelope).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode())

        def _path(self):
            seen_headers.append({
                "method": self.command,
                "token": self.headers.get("token"),
                "content-type": self.headers.get("Content-Type"),
            })
            prefix = f"/api/{APP_ID}/"
            if not self.path.startswith(prefix):
                return None
            return self.path[len(prefix):]

        # -- routes ----------------------------------------------------

        def do_GET(self):
            path = self._path()
            if path is None:
                return self._send("bad app id", status=404, success=False)

            if path == "sections/":
                return self._send(store.get_sections())

            # phpIPAM answers an empty collection with 404, not [].
            if path in ("l2domains/", "vlans/", "vrfs/"):
                getter = {
                    "l2domains/": store.get_l2domains,
                    "vlans/": store.get_vlans,
                    "vrfs/": store.get_vrfs,
                }[path]
                records = getter()
                if not records:
                    noun = path.strip("/")
                    return self._send(f"No {noun} configured",
                                      status=404, success=False)
                return self._send(records)

            match = re.fullmatch(r"sections/(\d+)/subnets/", path)
            if match:
                return self._send(store.get_subnets_in_section(match.group(1)))

            match = re.fullmatch(r"subnets/(\d+)/addresses/", path)
            if match:
                return self._send(store.get_addresses_in_subnet(match.group(1)))

            return self._send(f"no route for {path}", status=404, success=False)

        def do_POST(self):
            path = self._path()
            body = self._body()

            if path == "subnets/":
                extra = {
                    key: value for key, value in body.items()
                    if key not in ("subnet", "mask", "sectionId")
                }
                return self._send(store.create_subnet(
                    subnet=body["subnet"], mask=body["mask"],
                    section_id=body["sectionId"], **extra,
                ))

            if path == "addresses/":
                extra = {
                    key: value for key, value in body.items()
                    if key not in ("subnetId", "ip")
                }
                return self._send(store.create_address(
                    subnet_id=body["subnetId"], ip=body["ip"], **extra,
                ))

            if path == "sections/":
                return self._send(store.create_section(body["name"]))

            if path == "l2domains/":
                return self._send(store.create_l2domain(
                    body["name"],
                    sections=str(body.get("permissions") or "").split(";"),
                ))

            if path == "vlans/":
                extra = {k: v for k, v in body.items()
                         if k not in ("number", "name", "domainId")}
                return self._send(store.create_vlan(
                    number=body["number"], name=body.get("name", ""),
                    domain_id=body["domainId"], **extra))

            if path == "vrfs/":
                extra = {k: v for k, v in body.items()
                         if k not in ("name", "sections")}
                return self._send(store.create_vrf(
                    name=body["name"],
                    sections=str(body.get("sections") or "").split(";"),
                    **extra))

            return self._send(f"no route for {path}", status=404, success=False)

        def do_PATCH(self):
            path = self._path()
            body = self._body()

            match = re.fullmatch(r"subnets/(\d+)/", path)
            if match:
                store.update_subnet(match.group(1), **body)
                return self._send(None)

            match = re.fullmatch(r"addresses/(\d+)/", path)
            if match:
                store.update_address(match.group(1), **body)
                return self._send(None)

            match = re.fullmatch(r"l2domains/(\d+)/", path)
            if match:
                sections = body.pop("permissions", None)
                store.update_l2domain(
                    match.group(1),
                    sections=(str(sections).split(";") if sections else None),
                    **body)
                return self._send(None)

            match = re.fullmatch(r"vlans/(\d+)/", path)
            if match:
                store.update_vlan(match.group(1), **body)
                return self._send(None)

            match = re.fullmatch(r"vrfs/(\d+)/", path)
            if match:
                sections = body.pop("sections", None)
                store.update_vrf(
                    match.group(1),
                    sections=(str(sections).split(";") if sections else None),
                    **body)
                return self._send(None)

            return self._send(f"no route for {path}", status=404, success=False)

    return Handler


class TestHttpRoundTrip(unittest.TestCase):
    def setUp(self):
        self.store = FakePhpIpam()
        self.store.add_section("Shared")
        self.seen_headers = []

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.store, self.seen_headers)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        host, port = self.server.server_address[:2]
        self.client = SyncPhpIpamClient(
            base_url=f"http://{host}:{port}", app_id=APP_ID, token=TOKEN,
        )

        self.config = make_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        source, _ = populated_source()
        run_export(source, self.tmp.name, self.config)

    def _import(self, apply=True):
        _, documents = read_snapshot(self.tmp.name)
        documents = [canonicalise_document_cidr(d) for d in documents]
        view = TargetView(self.client)
        actions = build_plan(documents, view, self.config)
        if not apply:
            return actions, 0, []
        executor = Executor(self.client, view, self.config, lambda message: None)
        applied, errors = executor.apply(actions)
        return actions, applied, errors

    def test_import_over_http_creates_records(self):
        actions, applied, errors = self._import()
        self.assertEqual(errors, [])
        # 2 subnets, 2 addresses, 1 vlan, and the link that points the
        # subnet at the target's own id for it
        self.assertEqual(applied, 6)

        child = self.store.subnet_by_cidr("10.20.5.0/24")
        self.assertIsNotNone(child)
        self.assertEqual(child["description"], "Shared /24")
        self.assertEqual(child["custom_Owner"], "netops")
        ips = {a["ip"] for a in self.store.get_addresses_in_subnet(child["id"])}
        self.assertEqual(ips, {"10.20.5.2", "10.20.5.10"})

    def test_token_header_is_sent_on_every_request(self):
        self._import()
        self.assertTrue(self.seen_headers, "no requests reached the server")
        seen = {header["token"] for header in self.seen_headers}
        self.assertEqual(
            seen, {TOKEN},
            f"expected every request to carry the app token, got {seen}",
        )

    def test_json_content_type_is_sent_even_on_bodyless_requests(self):
        """phpIPAM <= 1.7.x on PHP 8 rejects an empty Content-Type with
        `415 Invalid Content type `, so every request must name it.

        Asserted per method: POSTs get the header from requests' json=
        handling, so an aggregate check would pass on writes alone."""
        self._import()

        gets = [h for h in self.seen_headers if h["method"] == "GET"]
        self.assertTrue(gets, "no bodyless GET reached the server")
        for header in self.seen_headers:
            self.assertEqual(
                header["content-type"], "application/json",
                f"{header['method']} request omitted the JSON content type "
                f"(got {header['content-type']!r}) -- phpIPAM <= 1.7.x on "
                f"PHP 8 answers 415 to that",
            )

    def test_nesting_survives_the_http_path(self):
        self._import()
        parent = self.store.subnet_by_cidr("10.20.0.0/16")
        child = self.store.subnet_by_cidr("10.20.5.0/24")
        self.assertEqual(child["masterSubnetId"], parent["id"])

    def test_second_import_over_http_is_a_no_op(self):
        self._import()
        self.store.write_calls.clear()
        actions, applied, errors = self._import()
        self.assertEqual(applied, 0)
        self.assertEqual(self.store.write_calls, [])

    def test_update_propagates_over_http(self):
        self._import()
        # Change the target out from under the snapshot, then re-sync:
        # the importer must correct it back.
        child = self.store.subnet_by_cidr("10.20.5.0/24")
        self.store.update_subnet(child["id"], description="drifted locally")
        self.store.write_calls.clear()

        actions, applied, errors = self._import()
        self.assertEqual(errors, [])
        self.assertEqual(applied, 1)
        self.assertEqual(
            self.store.subnet_by_cidr("10.20.5.0/24")["description"],
            "Shared /24",
        )

    def test_dry_run_over_http_sends_no_writes(self):
        self._import(apply=False)
        self.assertEqual(self.store.write_calls, [])
        self.assertIsNone(self.store.subnet_by_cidr("10.20.5.0/24"))


if __name__ == "__main__":
    unittest.main()
