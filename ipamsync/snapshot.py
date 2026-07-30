"""
ipamsync.snapshot

The on-disk format that crosses the one-way git mirror, plus the code to
write and read it.

## Layout

    manifest.json
    sections/<section-slug>/<subnet-slug>.json

One file per subnet, holding the subnet's own fields and every address
inside it. One-file-per-subnet (rather than a single big document) is
deliberate: the git history then shows *which subnet* changed in the
commit summary, and two subnets changing independently produce
non-overlapping diffs.

## Canonical serialisation

Every file is written with sorted keys, fixed indentation, addresses in
numeric IP order, and a trailing newline. A re-export with no upstream
changes must therefore produce byte-identical files, so the exporter can
tell "nothing changed" from "something changed" by asking git, and a
human reading the diff sees only real changes -- not dictionary
reordering.

## Why checksums

The transport is a one-way git mirror, so the importing side has no way
to ask the source to resend anything. A partially-mirrored or truncated
tree would otherwise be applied as if it were complete. `manifest.json`
records a SHA-256 for every subnet file and the importer verifies all of
them before touching the target instance, turning a corrupt transfer into
a loud failure instead of a silent partial sync.

The manifest is also authoritative about *membership*: a subnet file on
disk that the manifest does not list is stale (left by an older export)
and is ignored with a warning rather than imported.

## What the checksums do and do not prove

They prove the tree arrived intact. They prove nothing about *who wrote
it*: the manifest lists both the paths and their digests, so anyone able
to commit to the mirrored repo can write a manifest that is perfectly
self-consistent. Integrity, not authenticity.

That is why `_safe_relative_path` below refuses to follow a manifest
entry out of the snapshot directory, rather than trusting the path
because its digest matched. Without that check a manifest could name
`../../../../etc/passwd` with a matching digest and the importer would
read it, parse it, and push its contents into the target instance as a
subnet description -- turning "can commit to the mirror" into "can read
files off the importing host". Authenticity is the transport's job: see
"Trust model" in the README.
"""

import hashlib
import ipaddress
import json
import ntpath
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

#: Bumped only for a breaking change to the file layout or semantics.
#: The importer refuses a snapshot it does not recognise rather than
#: guessing at fields that may have changed meaning.
SCHEMA_VERSION = 1

MANIFEST_NAME = "manifest.json"
SECTIONS_DIR = "sections"


class SnapshotError(Exception):
    """Raised for a malformed, incomplete, or unreadable snapshot."""


def _dumps(obj):
    """Canonical JSON: sorted keys, stable indent, trailing newline."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _slug(text):
    """Filesystem-safe slug. Not required to be reversible -- every file
    carries its own canonical section name and CIDR internally; this is
    only about producing readable, stable paths.

    The dot handling is not cosmetic. `.` and `-` have to survive for
    ordinary names (`10.20.0.0_24`, `dmz-edge`), but a section named
    literally `..` would otherwise slug to `..` and place its file
    *outside* `sections/` -- so a section name, which is free text an
    operator types into phpIPAM, could write anywhere the exporter can
    reach. Any slug that is nothing but dots is therefore refused a
    path of its own, and a leading dot is dropped so no name produces a
    hidden file.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text).strip().lower())
    slug = slug.strip("-").lstrip(".")
    return slug or "unnamed"


def subnet_slug(cidr):
    """'10.20.0.0/24' -> '10.20.0.0_24'; '2001:db8::/48' -> '2001-db8--_48'."""
    return _slug(str(cidr).replace("/", "_").replace(":", "-"))


def canonical_cidr(subnet, mask):
    """Builds the natural key for a subnet from phpIPAM's two separate
    network/prefix fields, normalised through the stdlib so that e.g.
    '10.20.0.0/24' and '10.20.000.0/24' cannot become two different keys
    for the same network."""
    network = ipaddress.ip_network(f"{subnet}/{mask}", strict=False)
    return str(network)


def _ip_sort_key(ip):
    """Sorts addresses numerically, not lexically -- so .2 precedes .10
    and the diff of an address list reads in network order."""
    try:
        parsed = ipaddress.ip_address(ip)
        return (parsed.version, int(parsed))
    except ValueError:
        # Never expected from phpIPAM, but a malformed value must not
        # crash an export that is otherwise fine; sort it last.
        return (99, 0)


def build_subnet_document(*, section_name, cidr, fields, addresses,
                          master_subnet=None, vlan=None):
    """Assembles one subnet's document. `addresses` is an iterable of
    (ip, fields) pairs; they are sorted here so callers need not care."""
    return {
        "schema_version": SCHEMA_VERSION,
        "section": section_name,
        "cidr": cidr,
        "master_subnet": master_subnet,
        "vlan": vlan,
        "fields": fields,
        "addresses": [
            {"ip": ip, "fields": address_fields}
            for ip, address_fields in sorted(
                addresses, key=lambda pair: _ip_sort_key(pair[0])
            )
        ],
    }


def write_snapshot(out_dir, documents, *, source, sections, dropped_fields=None):
    """Writes a complete snapshot to `out_dir`, replacing whatever was
    there before.

    `documents` is the list of subnet documents from
    build_subnet_document(). Files under `sections/` that are not part of
    this export are deleted, so a subnet removed from the source section
    disappears from the snapshot (and therefore shows as a deletion in
    the git diff). Note this is about the *snapshot*, not the target
    instance -- the importer is additive and never deletes target
    records; it reports them as drift instead.

    Returns the manifest that was written.
    """
    out_dir = Path(out_dir)
    sections_root = out_dir / SECTIONS_DIR
    sections_root.mkdir(parents=True, exist_ok=True)

    files = {}
    written_paths = set()
    for document in documents:
        rel = (
            f"{SECTIONS_DIR}/{_slug(document['section'])}/"
            f"{subnet_slug(document['cidr'])}.json"
        )
        if rel in files:
            # Two different section names or CIDRs slugging to one path
            # would silently drop a subnet. Refuse rather than lose data.
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
        "subnet_count": len(documents),
        "address_count": sum(len(d["addresses"]) for d in documents),
        "dropped_source_fields": dropped_fields or {},
        "files": dict(sorted(files.items())),
    }

    # Carry the previous timestamp forward when nothing else changed.
    #
    # Without this the manifest differs on EVERY export purely because
    # `exported_at` moved, so the whole "commit only on real change"
    # property collapses: a five-minute cron produces ~288 empty commits
    # a day, and a genuine change becomes impossible to spot in the log.
    # The subnet files were already byte-stable; the manifest was the one
    # thing quietly breaking it.
    #
    # The cost is that `exported_at` means "when the content last
    # changed", NOT "when the exporter last ran" -- so it cannot be used
    # to prove the exporter is still alive. Nothing that crosses a
    # change-only channel can. Monitor the export job on Instance 1
    # directly (cron exit status) for that; see README.
    previous = _read_manifest_if_valid(manifest_path)
    if previous is not None and _same_content(previous, manifest):
        manifest["exported_at"] = previous.get(
            "exported_at", manifest["exported_at"]
        )

    manifest_path.write_bytes(_dumps(manifest).encode("utf-8"))
    return manifest


def _read_manifest_if_valid(path):
    """Returns the manifest already on disk, or None if absent/unreadable.
    A corrupt previous manifest simply means we cannot compare against it,
    which is not a reason to fail the export."""
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

    `rel` comes out of `manifest.json`, which crosses the one-way mirror
    from another machine -- so it is input, not a constant, and it is
    checked like input. An entry must be a relative path under
    `sections/` naming a `.json` file, and must still be under `in_dir`
    once symlinks are resolved.

    Note that `Path("/snapshot") / "/etc/shadow"` is `/etc/shadow` --
    pathlib discards the left side entirely for an absolute right side,
    so an absolute entry needs rejecting explicitly rather than being
    caught by a prefix check after the join.
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

    # Belt and braces: a symlink inside the tree could still redirect out
    # of it, and that is only visible after resolution.
    root = Path(in_dir).resolve()
    path = (root / pure).resolve()
    if path != root and root not in path.parents:
        raise SnapshotError(
            f"{MANIFEST_NAME} lists {rel!r}, which resolves to {path} -- "
            f"outside the snapshot directory {root}. Refusing to read it."
        )
    return path


def read_snapshot(in_dir):
    """Reads and verifies a snapshot. Returns (manifest, documents).

    Raises SnapshotError if the manifest is missing, the schema version
    is unrecognised, a listed file is absent, or any checksum fails --
    all of which mean the snapshot must not be applied.
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
        # Validate the path BEFORE reading it -- the digest cannot vouch
        # for a path, since whoever wrote the manifest chose both.
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
    """Returns snapshot files present on disk but absent from the
    manifest -- leftovers from an older export that the importer must
    ignore. Reported rather than deleted: the importing side may not own
    the mirrored working tree."""
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
