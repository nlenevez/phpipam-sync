#!/usr/bin/env python3
"""
ipam_export.py -- runs on Instance 1 (the source of truth).

Reads the configured phpIPAM section(s) over the REST API and writes a
canonical snapshot into the git repo that is mirrored one-way to
Instance 2. Read-only against phpIPAM: this script never creates,
updates, or deletes anything on the source instance.

    # write the snapshot, show what changed, commit nothing
    ./ipam_export.py --config config.yml --out-dir ../ipam-data

    # write, commit and push (the normal cron invocation)
    ./ipam_export.py --config config.yml --out-dir ../ipam-data --commit --push

Exit status is 0 on success (whether or not anything changed), 1 on
failure. `--fail-on-empty` additionally fails when a configured section
turns out to hold no subnets, which on a section that is supposed to
have content usually means a permissions problem on the API app rather
than a genuinely empty section -- worth catching before an empty
snapshot is mirrored.
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync import model                                    # noqa: E402
from ipamsync.config import ConfigError, build_client, load_config  # noqa: E402
from ipamsync.snapshot import (                               # noqa: E402
    SnapshotError, build_subnet_document, canonical_cidr, write_snapshot,
)


def log(message):
    print(message, flush=True)


def _vlan_for(client, subnet, cache):
    """Resolves a subnet's VLAN to a {number, name} record for the
    snapshot. VLANs are not replicated (out of scope by design), but the
    number is recorded so the importer can point out that the target
    subnet will not have it -- silently dropping the information on a
    one-way link would make it unrecoverable."""
    vlan_id = subnet.get("vlanId")
    if not vlan_id or str(vlan_id) in ("0", ""):
        return None
    if vlan_id not in cache:
        try:
            vlan = client.get_vlan(vlan_id) or {}
            cache[vlan_id] = {
                "number": str(vlan.get("number", "")),
                "name": vlan.get("name") or "",
            }
        except Exception as exc:  # noqa: BLE001
            log(f"  warning: could not read VLAN id {vlan_id}: {exc}")
            cache[vlan_id] = None
    return cache[vlan_id]


def _discover_custom_fields(getter, sample_id, kind):
    """Learns the custom-field names for one table by reading a single
    record.

    Needed because phpIPAM's *list* endpoints omit the `custom_fields`
    block entirely for records whose custom fields are all null, leaking
    the raw columns at top level instead -- where they are
    indistinguishable from phpIPAM fields this tool has not been taught
    about. The single-record endpoint always nests, so one probe per
    table gives the authoritative name list.

    Costs two extra API calls per run, total. Returns an empty set on any
    failure: not knowing the names is a degraded mode (unknown fields get
    reported rather than carried), not a reason to abort an export.
    """
    try:
        record = getter(sample_id) or {}
    except Exception as exc:  # noqa: BLE001
        log(f"  note: could not probe {kind} custom fields: {exc}")
        return set()
    nested = record.get(model.NESTED_CUSTOM_FIELDS_KEY)
    if not isinstance(nested, dict):
        return set()
    return set(nested)


def export(config, out_dir, fail_on_empty=False):
    client = build_client(config, "source")
    options = config["options"]

    documents = []
    dropped_fields = {"subnet": set(), "address": set()}
    # Whether phpIPAM is telling us which fields are custom (see
    # ipamsync.model.NESTED_CUSTOM_FIELDS_KEY). Decides which advice
    # the dropped-field report gives at the end of the run.
    saw_nested_custom_fields = False
    # Custom-field names per table, learned once from a single-record
    # read; see _discover_custom_fields.
    custom_names = {"subnet": set(), "address": set()}
    vlan_cache = {}

    all_sections = client.get_sections()
    by_name = {
        str(section.get("name", "")).strip().casefold(): section
        for section in all_sections
    }

    for section_name in config["sections"]:
        section = by_name.get(section_name.strip().casefold())
        if not section:
            available = ", ".join(sorted(
                str(s.get("name")) for s in all_sections
            )) or "(none visible to this API app)"
            raise ConfigError(
                f"Source instance has no section named {section_name!r}. "
                f"Sections visible to this API app: {available}"
            )

        section_id = section["id"]
        subnets = client.get_subnets_in_section(section_id)
        log(f"section {section_name!r} (id {section_id}): {len(subnets)} subnet(s)")

        if not subnets and fail_on_empty:
            raise ConfigError(
                f"Section {section_name!r} contains no subnets. If that is "
                f"genuinely correct, drop --fail-on-empty; otherwise check "
                f"the API app's permissions on this section."
            )

        if subnets and not custom_names["subnet"]:
            custom_names["subnet"] = _discover_custom_fields(
                client.get_subnet, subnets[0]["id"], "subnet")
            if custom_names["subnet"]:
                saw_nested_custom_fields = True
                log(f"  custom subnet field(s): "
                    f"{', '.join(sorted(custom_names['subnet']))}")

        # id -> CIDR, so masterSubnetId can be recorded as a natural key.
        cidr_by_id = {}
        for raw in subnets:
            if raw.get("subnet") and raw.get("mask") not in (None, ""):
                try:
                    cidr_by_id[str(raw["id"])] = canonical_cidr(
                        raw["subnet"], raw["mask"]
                    )
                except ValueError:
                    pass

        for raw in subnets:
            if not raw.get("subnet") or raw.get("mask") in (None, ""):
                log(f"  skipping subnet id {raw.get('id')} -- no network/mask "
                    f"(phpIPAM folder?)")
                continue
            try:
                cidr = canonical_cidr(raw["subnet"], raw["mask"])
            except ValueError as exc:
                log(f"  skipping subnet id {raw.get('id')} -- unparseable "
                    f"network {raw.get('subnet')}/{raw.get('mask')}: {exc}")
                continue

            if isinstance(raw.get(model.NESTED_CUSTOM_FIELDS_KEY), dict):
                saw_nested_custom_fields = True
            fields, dropped = model.partition_subnet(
                raw, options, custom_names["subnet"])
            dropped_fields["subnet"].update(dropped)

            master_id = raw.get("masterSubnetId")
            master_subnet = None
            if master_id and str(master_id) not in ("0", ""):
                master_subnet = cidr_by_id.get(str(master_id))
                if master_subnet is None:
                    log(f"  note: {cidr} nests under subnet id {master_id}, "
                        f"which is outside the replicated section -- it will "
                        f"be created top-level on the target")

            addresses = []
            raw_addresses = client.get_addresses_in_subnet(raw["id"])
            if raw_addresses and not custom_names["address"]:
                custom_names["address"] = _discover_custom_fields(
                    client.get_address, raw_addresses[0]["id"], "address")
                if custom_names["address"]:
                    saw_nested_custom_fields = True
                    log(f"  custom address field(s): "
                        f"{', '.join(sorted(custom_names['address']))}")
            for raw_address in raw_addresses:
                ip = raw_address.get("ip")
                if not ip:
                    continue
                if isinstance(raw_address.get(model.NESTED_CUSTOM_FIELDS_KEY), dict):
                    saw_nested_custom_fields = True
                address_fields, address_dropped = model.partition_address(
                    raw_address, options, custom_names["address"]
                )
                dropped_fields["address"].update(address_dropped)
                addresses.append((str(ip), address_fields))

            documents.append(build_subnet_document(
                section_name=section_name,
                cidr=cidr,
                fields=fields,
                addresses=addresses,
                master_subnet=master_subnet,
                vlan=_vlan_for(client, raw, vlan_cache),
            ))
            log(f"  {cidr}: {len(addresses)} address(es)")

    source_side = config["source"]
    manifest = write_snapshot(
        out_dir,
        documents,
        source={
            "base_url": source_side.get("base_url"),
            "app_id": source_side.get("app_id"),
        },
        sections=config["sections"],
        dropped_fields={
            kind: sorted(names) for kind, names in dropped_fields.items() if names
        },
    )

    for kind, names in sorted(dropped_fields.items()):
        if names:
            log(f"note: {kind} field(s) present on the source but not "
                f"replicated: {', '.join(sorted(names))}")
            if not saw_nested_custom_fields:
                # Much the most likely explanation, and a one-click fix.
                log(f"      These may be CUSTOM FIELDS. phpIPAM only "
                    f"identifies custom fields distinctly when the API app "
                    f"has 'nest custom fields' enabled -- without it a "
                    f"custom field named e.g. 'Owner' is indistinguishable "
                    f"from a phpIPAM field this tool does not know.")
                log(f"      Enable it on the SOURCE instance: Administration "
                    f"> API > edit app '{source_side.get('app_id')}' > "
                    f"Nest custom fields = Yes, then re-run.")
            else:
                log(f"      (custom-field nesting IS enabled, so these are "
                    f"phpIPAM's own fields. See ipamsync/model.py -- add "
                    f"them to the allowlist if they are literal values "
                    f"rather than local ids.)")

    return manifest


def git(args, cwd):
    """Runs a git command in the snapshot repo, raising on failure."""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def commit_snapshot(out_dir, manifest, push=False):
    """Commits the snapshot, and pushes if asked.

    Commits only when something actually changed. Because the snapshot is
    canonically serialised, an unchanged export produces byte-identical
    files and git reports a clean tree -- so a cron running this every
    few minutes does not fill the mirrored repo with empty commits.
    """
    status = git(["status", "--porcelain", "."], cwd=out_dir).strip()
    if not status:
        log("snapshot unchanged -- nothing to commit")
        return False

    changed = len([line for line in status.splitlines() if line.strip()])
    git(["add", "-A", "."], cwd=out_dir)
    message = (
        f"ipam snapshot: {manifest['subnet_count']} subnet(s), "
        f"{manifest['address_count']} address(es), {changed} file(s) changed"
    )
    git(["commit", "-m", message], cwd=out_dir)
    log(f"committed: {message}")

    if push:
        git(["push"], cwd=out_dir)
        log("pushed to the mirrored remote")
    return True


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--out-dir", required=True,
                        help="Snapshot root -- a path inside the working copy "
                             "of the git repo that is mirrored to Instance 2.")
    parser.add_argument("--commit", action="store_true",
                        help="git-commit the snapshot if it changed.")
    parser.add_argument("--push", action="store_true",
                        help="git-push after committing. Implies --commit.")
    parser.add_argument("--fail-on-empty", action="store_true",
                        help="Exit non-zero if a configured section holds no "
                             "subnets (usually an API permissions problem).")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        manifest = export(config, args.out_dir, fail_on_empty=args.fail_on_empty)
        log(f"snapshot written to {args.out_dir}: "
            f"{manifest['subnet_count']} subnet(s), "
            f"{manifest['address_count']} address(es)")
        if args.commit or args.push:
            commit_snapshot(args.out_dir, manifest, push=args.push)
    except (ConfigError, SnapshotError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
