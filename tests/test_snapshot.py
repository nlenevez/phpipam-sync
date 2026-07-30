"""
Tests for the on-disk snapshot format.

The properties that matter here are the ones the one-way link depends
on: a re-export with no changes must be byte-identical (or git churns
and real changes become invisible), and a damaged or incomplete snapshot
must be refused rather than half-applied.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipamsync import snapshot  # noqa: E402


def make_document(cidr="10.20.0.0/24", section="Shared", addresses=None,
                  master_subnet=None):
    return snapshot.build_subnet_document(
        section_name=section,
        cidr=cidr,
        fields={"description": "core"},
        addresses=addresses if addresses is not None else [
            ("10.20.0.10", {"hostname": "sw10"}),
            ("10.20.0.2", {"hostname": "sw2"}),
        ],
        master_subnet=master_subnet,
    )


SOURCE = {"base_url": "https://ipam1.example.com", "app_id": "sync"}


class TestCanonicalCidr(unittest.TestCase):
    def test_normalises_equivalent_spellings_to_one_key(self):
        self.assertEqual(
            snapshot.canonical_cidr("10.20.0.0", "24"),
            snapshot.canonical_cidr("10.20.0.0", 24),
        )

    def test_host_bits_are_normalised_away(self):
        # phpIPAM stores the network address, but a hand-edited snapshot
        # must not produce a second, non-matching key for one network.
        self.assertEqual(snapshot.canonical_cidr("10.20.0.5", "24"), "10.20.0.0/24")

    def test_negative_control_different_networks_stay_different(self):
        self.assertNotEqual(
            snapshot.canonical_cidr("10.20.0.0", "24"),
            snapshot.canonical_cidr("10.20.1.0", "24"),
        )


class TestAddressOrdering(unittest.TestCase):
    def test_addresses_sort_numerically_not_lexically(self):
        document = make_document()
        ips = [entry["ip"] for entry in document["addresses"]]
        self.assertEqual(ips, ["10.20.0.2", "10.20.0.10"])


class TestSlugs(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(snapshot.subnet_slug("10.20.0.0/24"), "10.20.0.0_24")

    def test_ipv6_has_no_colons(self):
        slug = snapshot.subnet_slug("2001:db8::/48")
        self.assertNotIn(":", slug)
        self.assertNotIn("/", slug)


class TestWriteAndRead(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = snapshot.write_snapshot(
                tmp, [make_document()], source=SOURCE, sections=["Shared"],
            )
            self.assertEqual(manifest["subnet_count"], 1)
            self.assertEqual(manifest["address_count"], 2)

            read_manifest, documents = snapshot.read_snapshot(tmp)
            self.assertEqual(read_manifest["subnet_count"], 1)
            self.assertEqual(documents[0]["cidr"], "10.20.0.0/24")

    def test_reexport_is_byte_identical(self):
        # The whole "commit only when something changed" behaviour rests
        # on this: unchanged data must serialise to identical bytes.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            first = (Path(tmp) / "sections/shared/10.20.0.0_24.json").read_bytes()
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            second = (Path(tmp) / "sections/shared/10.20.0.0_24.json").read_bytes()
            self.assertEqual(first, second)

    def test_manifest_is_byte_identical_when_nothing_changed(self):
        # Regression: `exported_at` moved on every export, so the manifest
        # always differed and the exporter committed on every single run.
        # At a five-minute cron that is ~288 empty commits a day, and a
        # real change becomes impossible to spot in the log. The subnet
        # files were already stable; the manifest was the leak.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            first = (Path(tmp) / snapshot.MANIFEST_NAME).read_bytes()
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            second = (Path(tmp) / snapshot.MANIFEST_NAME).read_bytes()
            self.assertEqual(first, second)

    def test_negative_control_manifest_changes_when_content_changes(self):
        # If the timestamp were simply frozen, the manifest would stop
        # tracking reality. It must still move when the data does.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            first = (Path(tmp) / snapshot.MANIFEST_NAME).read_bytes()
            snapshot.write_snapshot(
                tmp,
                [make_document(addresses=[("10.20.0.2", {"hostname": "new"})])],
                source=SOURCE, sections=["Shared"],
            )
            second = (Path(tmp) / snapshot.MANIFEST_NAME).read_bytes()
            self.assertNotEqual(first, second)

    def test_whole_snapshot_is_clean_on_a_no_change_export(self):
        # The property the exporter's commit-only-on-change logic rests
        # on, checked across every file rather than just one.
        with tempfile.TemporaryDirectory() as tmp:
            docs = [make_document(), make_document(cidr="10.20.1.0/24")]
            snapshot.write_snapshot(tmp, docs, source=SOURCE, sections=["Shared"])
            before = {p: p.read_bytes() for p in Path(tmp).rglob("*.json")}
            snapshot.write_snapshot(tmp, docs, source=SOURCE, sections=["Shared"])
            after = {p: p.read_bytes() for p in Path(tmp).rglob("*.json")}
            self.assertEqual(before, after)

    def test_negative_control_changed_data_changes_the_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            first = (Path(tmp) / "sections/shared/10.20.0.0_24.json").read_bytes()
            changed = make_document(addresses=[("10.20.0.2", {"hostname": "sw2-new"})])
            snapshot.write_snapshot(tmp, [changed], source=SOURCE,
                                    sections=["Shared"])
            second = (Path(tmp) / "sections/shared/10.20.0.0_24.json").read_bytes()
            self.assertNotEqual(first, second)

    def test_removed_subnet_is_pruned_from_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(
                tmp,
                [make_document(), make_document(cidr="10.20.1.0/24")],
                source=SOURCE, sections=["Shared"],
            )
            self.assertTrue((Path(tmp) / "sections/shared/10.20.1.0_24.json").exists())

            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            self.assertFalse((Path(tmp) / "sections/shared/10.20.1.0_24.json").exists())
            _, documents = snapshot.read_snapshot(tmp)
            self.assertEqual(len(documents), 1)


class TestCorruptSnapshotsAreRefused(unittest.TestCase):
    """Every one of these is a negative control for the checksum/manifest
    machinery: if any of them silently succeeded, a truncated mirror
    transfer would be applied to the target as if it were complete."""

    def _written(self, tmp):
        return snapshot.write_snapshot(
            tmp,
            [make_document(), make_document(cidr="10.20.1.0/24")],
            source=SOURCE, sections=["Shared"],
        )

    def test_tampered_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._written(tmp)
            path = Path(tmp) / "sections/shared/10.20.0.0_24.json"
            document = json.loads(path.read_text())
            document["fields"]["description"] = "tampered"
            path.write_text(json.dumps(document))
            with self.assertRaises(snapshot.SnapshotError) as ctx:
                snapshot.read_snapshot(tmp)
            self.assertIn("Checksum mismatch", str(ctx.exception))

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._written(tmp)
            (Path(tmp) / "sections/shared/10.20.1.0_24.json").unlink()
            with self.assertRaises(snapshot.SnapshotError) as ctx:
                snapshot.read_snapshot(tmp)
            self.assertIn("missing", str(ctx.exception))

    def test_absent_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.read_snapshot(tmp)

    def test_unknown_schema_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._written(tmp)
            manifest_path = Path(tmp) / snapshot.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest["schema_version"] = 99
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(snapshot.SnapshotError) as ctx:
                snapshot.read_snapshot(tmp)
            self.assertIn("schema_version", str(ctx.exception))

    def test_stale_unlisted_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._written(tmp)
            stray = Path(tmp) / "sections/shared/10.20.9.0_24.json"
            stray.write_text("{}")
            self.assertIn(
                "sections/shared/10.20.9.0_24.json",
                snapshot.find_stale_files(tmp, manifest),
            )

    def test_negative_control_an_intact_snapshot_reads_cleanly(self):
        # Proves the rejections above are triggered by the damage, not by
        # read_snapshot() being broken for everything.
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._written(tmp)
            read_manifest, documents = snapshot.read_snapshot(tmp)
            self.assertEqual(len(documents), 2)
            self.assertEqual(read_manifest["files"], manifest["files"])
            self.assertEqual(snapshot.find_stale_files(tmp, manifest), [])


class TestManifestPathsAreNotTrusted(unittest.TestCase):
    """A manifest crosses the one-way mirror from another machine, so its
    file paths are input.

    The checksums cannot police them: whoever writes the manifest chooses
    both the path and the digest it must match, so a self-consistent
    manifest naming a file outside the snapshot is trivial to produce.
    Left unchecked, "can commit to the mirrored repo" escalates to "can
    read arbitrary files off the importing host, and have their contents
    written into phpIPAM as subnet data".
    """

    def _snapshot_with_entry(self, tmp, rel, payload_path):
        """Builds a valid snapshot, then rewrites the manifest to point one
        entry at `payload_path` under the name `rel`, digest included."""
        snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                sections=["Shared"])
        payload = Path(payload_path).read_bytes()
        import hashlib
        manifest_path = Path(tmp) / snapshot.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = {rel: hashlib.sha256(payload).hexdigest()}
        manifest_path.write_text(json.dumps(manifest))

    def test_refuses_a_traversal_path_even_with_a_matching_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret.json"
            outside.write_text(json.dumps({"fields": {"pw": "s3cr3t"}}))
            root = Path(tmp) / "snap"
            root.mkdir()
            self._snapshot_with_entry(root, "../secret.json", outside)
            with self.assertRaises(snapshot.SnapshotError) as caught:
                snapshot.read_snapshot(root)
            self.assertIn("outside the snapshot directory", str(caught.exception))

    def test_refuses_an_absolute_path(self):
        # pathlib discards the base for an absolute right-hand side, so
        # this cannot be caught by a prefix check after the join.
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret.json"
            outside.write_text(json.dumps({"fields": {}}))
            root = Path(tmp) / "snap"
            root.mkdir()
            self._snapshot_with_entry(root, str(outside), outside)
            with self.assertRaises(snapshot.SnapshotError) as caught:
                snapshot.read_snapshot(root)
            self.assertIn("absolute path", str(caught.exception))

    def test_refuses_a_path_outside_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "snap"
            root.mkdir()
            sneaky = root / "elsewhere.json"
            sneaky.parent.mkdir(parents=True, exist_ok=True)
            sneaky.write_text(json.dumps({"fields": {}}))
            self._snapshot_with_entry(root, "elsewhere.json", sneaky)
            with self.assertRaises(snapshot.SnapshotError) as caught:
                snapshot.read_snapshot(root)
            self.assertIn("not a sections/", str(caught.exception))

    def test_refuses_a_symlink_that_redirects_out_of_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret.json"
            outside.write_text(json.dumps({"fields": {}}))
            root = Path(tmp) / "snap"
            root.mkdir()
            snapshot.write_snapshot(root, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            link = root / snapshot.SECTIONS_DIR / "shared" / "linked.json"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this platform")
            import hashlib
            manifest_path = root / snapshot.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest["files"] = {
                f"{snapshot.SECTIONS_DIR}/shared/linked.json":
                    hashlib.sha256(outside.read_bytes()).hexdigest()
            }
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(snapshot.SnapshotError) as caught:
                snapshot.read_snapshot(root)
            self.assertIn("outside the snapshot directory", str(caught.exception))

    def test_negative_control_a_legitimate_path_still_reads(self):
        # The rejections above must come from the paths being hostile, not
        # from _safe_relative_path refusing everything.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(tmp, [make_document()], source=SOURCE,
                                    sections=["Shared"])
            _, documents = snapshot.read_snapshot(tmp)
            self.assertEqual(len(documents), 1)


class TestSlugCannotEscapeSectionsDir(unittest.TestCase):
    """Section names are free text an operator types into phpIPAM, and
    they become directory names. A name of `..` must not place a file
    outside `sections/`."""

    def test_dot_only_section_names_get_a_safe_slug(self):
        for hostile in ("..", ".", "...", "../"):
            with self.subTest(name=hostile):
                self.assertNotIn(snapshot._slug(hostile), ("..", ".", "..."))

    def test_a_dot_dot_section_writes_inside_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot.write_snapshot(
                tmp, [make_document(section="..")], source=SOURCE,
                sections=[".."],
            )
            manifest = json.loads(
                (Path(tmp) / snapshot.MANIFEST_NAME).read_text())
            for rel in manifest["files"]:
                self.assertTrue(rel.startswith(f"{snapshot.SECTIONS_DIR}/"), rel)
                self.assertNotIn("..", Path(rel).parts)
            # And it round-trips, rather than being written somewhere the
            # reader then refuses.
            _, documents = snapshot.read_snapshot(tmp)
            self.assertEqual(len(documents), 1)

    def test_ordinary_names_with_dots_are_untouched(self):
        # The fix must not mangle the normal case -- CIDR slugs are full
        # of dots and so are plenty of section names.
        self.assertEqual(snapshot._slug("Site-A.core"), "site-a.core")
        self.assertEqual(snapshot.subnet_slug("10.20.0.0/24"), "10.20.0.0_24")


if __name__ == "__main__":
    unittest.main()
