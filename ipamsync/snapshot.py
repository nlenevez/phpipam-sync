"""
ipamsync.snapshot

The on-disk snapshot format, and code to write and read it.

## Layout

    manifest.json
    sections/<section-slug>/_section.json
    sections/<section-slug>/<subnet-slug>.json

One file per subnet (fields plus addresses), so git diffs show which subnet
changed. Files are canonical (sorted keys, numeric IP order, trailing
newline), so an unchanged re-export is byte-identical.

## Checksums

The transport is one-way, so the importer cannot ask for a resend.
`manifest.json` records a SHA-256 per file, all verified before the target
is touched, so a truncated or partial mirror fails loudly. Files not listed
in the manifest are stale and ignored.

Checksums prove integrity, not authenticity: whoever writes the manifest
chooses both paths and digests. Hence `_safe_relative_path` confines
entries to the snapshot directory. Authenticity is the transport's job (see
"Trust model" in the README).
"""

import hashlib
import ipaddress
import json
import ntpath
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

#: Bumped for any change to layout or semantics, including ADDED keys: a
#: snapshot missing a key reads as "no such records", which under strict
#: mirror means deleting. The importer refuses an unknown version.
#:
#:   1  sections, subnets, addresses
#:   2  + `kind`, `_section.json` (L2 domains, VLANs, VRFs), vlan/vrf refs
#:   3  + `folders` in the section document, `parent_folder` on subnets
SCHEMA_VERSION = 3

MANIFEST_NAME = "manifest.json"
SECTIONS_DIR = "sections"


class SnapshotError(Exception):
    """Raised for a malformed, incomplete, or unreadable snapshot."""


def _dumps(obj):
    """Canonical JSON: sorted keys, stable indent, trailing newline."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _slug(text):
    """Filesystem-safe, non-reversible slug.

    Leading dots are dropped so a section named `..` cannot escape
    `sections/` and no name produces a hidden file.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text).strip().lower())
    slug = slug.strip("-").lstrip(".")
    return slug or "unnamed"


def subnet_slug(cidr):
    """'10.20.0.0/24' -> '10.20.0.0_24'; '2001:db8::/48' -> '2001-db8--_48'."""
    return _slug(str(cidr).replace("/", "_").replace(":", "-"))


def canonical_cidr(subnet, mask):
    """Canonical CIDR (a subnet's natural key) from phpIPAM's network and
    mask fields."""
    network = ipaddress.ip_network(f"{subnet}/{mask}", strict=False)
    return str(network)


def _ip_sort_key(ip):
    """Numeric IP sort key; malformed values sort last."""
    try:
        parsed = ipaddress.ip_address(ip)
        return (parsed.version, int(parsed))
    except ValueError:
        return (99, 0)


#: Per-section metadata file. The underscore cannot collide with a subnet
#: slug.
SECTION_META_NAME = "_section.json"


def build_section_document(*, section_name, domains, vlans, vrfs, folders=()):
    """Assembles the per-section document of folders, L2 domains, VLANs
    and VRFs.

    A folder path is a list of names, since folder names may contain "/".
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "section",
        "section": section_name,
        # Parents before children (creation order).
        "folders": sorted(
            (list(path) for path in folders),
            key=lambda path: (len(path), path),
        ),
        "vlan_domains": sorted(domains, key=lambda d: str(d.get("name") or "")),
        "vlans": sorted(
            vlans,
            key=lambda v: (str(v.get("domain") or ""), _vlan_sort_key(v)),
        ),
        "vrfs": sorted(vrfs, key=lambda v: str(v.get("name") or "")),
    }


def _vlan_sort_key(vlan):
    """Numeric VLAN sort key."""
    try:
        return (0, int(str(vlan.get("number"))))
    except (TypeError, ValueError):
        return (1, 0)


def build_subnet_document(*, section_name, cidr, fields, addresses,
                          master_subnet=None, vlan=None, vrf=None,
                          parent_folder=None):
    """Assembles one subnet's document. `addresses` is an iterable of
    (ip, fields) pairs, sorted here.

    `vlan` ({"domain", "number"}) and `vrf` ({"name"}) are natural-key
    references. The parent is either `master_subnet` (a CIDR) or
    `parent_folder` (a path), never both.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "subnet",
        "section": section_name,
        "cidr": cidr,
        "master_subnet": master_subnet,
        "parent_folder": list(parent_folder) if parent_folder else None,
        "vlan": vlan,
        "vrf": vrf,
        "fields": fields,
        "addresses": [
            {"ip": ip, "fields": address_fields}
            for ip, address_fields in sorted(
                addresses, key=lambda pair: _ip_sort_key(pair[0])
            )
        ],
    }


def write_snapshot(out_dir, documents, *, source, sections, dropped_fields=None):
    """Writes a complete snapshot to `out_dir`, removing files under
    `sections/` not in this export. Returns the manifest written."""
    out_dir = Path(out_dir)
    sections_root = out_dir / SECTIONS_DIR
    sections_root.mkdir(parents=True, exist_ok=True)
    subnet_docs, section_docs = partition_documents(documents)

    files = {}
    written_paths = set()
    for document in documents:
        if document.get("kind") == "section":
            rel = (
                f"{SECTIONS_DIR}/{_slug(document['section'])}/"
                f"{SECTION_META_NAME}"
            )
        else:
            rel = (
                f"{SECTIONS_DIR}/{_slug(document['section'])}/"
                f"{subnet_slug(document['cidr'])}.json"
            )
        if rel in files:
            raise SnapshotError(
                f"Snapshot path collision on {rel!r} -- two subnets slug to "
                f"the same filename (section {document['section']!r}, "
                f"CIDR {document['cidr']}). Rename one section to disambiguate."
            )
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _dumps(document).encode("utf-8")
        path.write_bytes(payload)
        files[rel] = hashlib.sha256(payload).hexdigest()
        written_paths.add(path.resolve())

    for stale in sections_root.rglob("*.json"):
        if stale.resolve() not in written_paths:
            stale.unlink()
    for directory in sorted(sections_root.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    manifest_path = out_dir / MANIFEST_NAME
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "sections": sorted(sections),
        "subnet_count": len(subnet_docs),
        "address_count": sum(len(d["addresses"]) for d in subnet_docs),
        "vlan_count": sum(len(d.get("vlans") or []) for d in section_docs),
        "vrf_count": sum(len(d.get("vrfs") or []) for d in section_docs),
        "dropped_source_fields": dropped_fields or {},
        "files": dict(sorted(files.items())),
    }

    # Keep the previous timestamp when nothing else changed, so an
    # unchanged export produces no commit. `exported_at` therefore means
    # "content last changed", not "exporter last ran".
    previous = _read_manifest_if_valid(manifest_path)
    if previous is not None and _same_content(previous, manifest):
        manifest["exported_at"] = previous.get(
            "exported_at", manifest["exported_at"]
        )

    manifest_path.write_bytes(_dumps(manifest).encode("utf-8"))
    return manifest


def _read_manifest_if_valid(path):
    """Returns the manifest already on disk, or None if absent/unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _same_content(previous, current):
    """True when two manifests differ only by `exported_at`."""
    ignore = {"exported_at"}
    return {k: v for k, v in previous.items() if k not in ignore} == \
           {k: v for k, v in current.items() if k not in ignore}


def _safe_relative_path(in_dir, rel):
    """Resolves a manifest-listed path inside the snapshot, or raises.

    `rel` is untrusted: it must be a relative `sections/...json` path that
    stays under `in_dir` after resolving symlinks. Absolute paths are
    rejected explicitly, since `Path(a) / "/x"` is `/x`.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise SnapshotError(
            f"{MANIFEST_NAME} lists a file entry that is not a path: {rel!r}."
        )

    pure = PurePosixPath(rel)
    if pure.is_absolute() or ntpath.isabs(rel) or "\\" in rel:
        raise SnapshotError(
            f"{MANIFEST_NAME} lists {rel!r}, an absolute path. Snapshot "
            f"entries must be relative to the snapshot directory; refusing "
            f"to read outside it."
        )
    if ".." in pure.parts:
        raise SnapshotError(
            f"{MANIFEST_NAME} lists {rel!r}, which points outside the "
            f"snapshot directory. Refusing to read it. A snapshot may only "
            f"reference files beneath its own {SECTIONS_DIR}/ directory."
        )
    if pure.parts[:1] != (SECTIONS_DIR,) or pure.suffix != ".json":
        raise SnapshotError(
            f"{MANIFEST_NAME} lists {rel!r}, which is not a "
            f"{SECTIONS_DIR}/....json path. Refusing to read it."
        )

    # A symlink could still redirect out of the tree.
    root = Path(in_dir).resolve()
    path = (root / pure).resolve()
    if path != root and root not in path.parents:
        raise SnapshotError(
            f"{MANIFEST_NAME} lists {rel!r}, which resolves to {path} -- "
            f"outside the snapshot directory {root}. Refusing to read it."
        )
    return path


def partition_documents(documents):
    """Splits a snapshot's documents into (subnets, section records)."""
    subnets, sections = [], []
    for document in documents:
        (sections if document.get("kind") == "section" else subnets).append(
            document
        )
    return subnets, sections


def read_snapshot(in_dir):
    """Reads and verifies a snapshot. Returns (manifest, documents).

    Raises SnapshotError on a missing manifest, unknown schema version,
    missing listed file, or checksum mismatch.
    """
    in_dir = Path(in_dir)
    manifest_path = in_dir / MANIFEST_NAME

    if not manifest_path.exists():
        raise SnapshotError(
            f"No {MANIFEST_NAME} in {in_dir} -- not a snapshot directory. "
            f"Check the path points at the mirrored repo's snapshot root, "
            f"and that the mirror has actually delivered a first export."
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SnapshotError(f"{manifest_path} is not valid JSON: {exc}")

    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SnapshotError(
            f"Snapshot schema_version is {version!r}, this importer "
            f"understands {SCHEMA_VERSION}. Upgrade the importer on this "
            f"instance to match the exporter on the source instance."
        )

    listed = manifest.get("files") or {}
    if not listed:
        raise SnapshotError(
            f"{manifest_path} lists no subnet files. If the source section "
            f"is genuinely empty this is expected -- rerun with --allow-empty "
            f"to accept it; otherwise the export failed partway."
        )

    documents = []
    for rel, expected_digest in sorted(listed.items()):
        # Validate the path before reading; the digest cannot vouch for it.
        path = _safe_relative_path(in_dir, rel)
        if not path.exists():
            raise SnapshotError(
                f"{MANIFEST_NAME} lists {rel} but it is missing from {in_dir}. "
                f"The mirror delivered an incomplete tree; refusing to import "
                f"a partial snapshot."
            )
        payload = path.read_bytes()
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != expected_digest:
            raise SnapshotError(
                f"Checksum mismatch for {rel}: manifest says "
                f"{expected_digest[:12]}..., file is {actual_digest[:12]}.... "
                f"The file was modified after export or corrupted in "
                f"transit; refusing to import."
            )
        try:
            documents.append(json.loads(payload.decode("utf-8")))
        except ValueError as exc:
            raise SnapshotError(f"{rel} is not valid JSON: {exc}")

    return manifest, documents


def find_stale_files(in_dir, manifest):
    """Returns snapshot files on disk but absent from the manifest.
    Reported, not deleted: the importer may not own the working tree."""
    in_dir = Path(in_dir)
    listed = {(in_dir / rel).resolve() for rel in (manifest.get("files") or {})}
    sections_root = in_dir / SECTIONS_DIR
    if not sections_root.exists():
        return []
    return sorted(
        str(path.relative_to(in_dir))
        for path in sections_root.rglob("*.json")
        if path.resolve() not in listed
    )
