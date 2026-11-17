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
failure. `--fail-on-empty` also fails when a configured section holds no
subnets, which usually means an API app permissions problem.
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync import model                                    # noqa: E402
from ipamsync.config import ConfigError, build_client, load_config  # noqa: E402
from ipamsync.snapshot import (                               # noqa: E402
    SnapshotError, build_section_document, build_subnet_document,
    canonical_cidr, write_snapshot,
)


def log(message):
    print(message, flush=True)


def _folder_paths(subnets):
    """Maps folder id -> path (list of names) for one section's folders.

    A folder is a `subnets` row with isFolder=1, named by `description`.
    Cycles are broken.
    """
    folders = {
        str(raw["id"]): raw for raw in subnets
        if str(raw.get("isFolder") or "0") == "1"
    }
    paths = {}

    def resolve(folder_id, seen):
        if folder_id in paths:
            return paths[folder_id]
        raw = folders.get(folder_id)
        if raw is None or folder_id in seen:
            return None
        seen.add(folder_id)
        name = str(raw.get("description") or "").strip()
        parent_id = str(raw.get("masterSubnetId") or "0")
        if parent_id in ("0", "", "None") or parent_id not in folders:
            path = [name]
        else:
            parent_path = resolve(parent_id, seen)
            path = ([*parent_path, name] if parent_path else [name])
        paths[folder_id] = path
        return path

    for folder_id in folders:
        resolve(folder_id, set())
    return paths


def _section_ids(raw_value):
    """Parses phpIPAM's ";"-separated section-id list ("3", "3;7", "",
    None)."""
    if raw_value in (None, "", "null"):
        return set()
    return {part.strip() for part in str(raw_value).split(";") if part.strip()}


def _l2_context(client, section_id, options):
    """L2 domains, VLANs and VRFs belonging to one source section.

    Returns (domains, vlans, vrfs, vlan_ref_by_id, vrf_name_by_id). Domain 1
    belongs to every section; others and VRFs by their section list.
    """
    section_id = str(section_id)
    domains, vlans, vrfs = [], [], {}
    vlan_ref_by_id, vrf_name_by_id = {}, {}

    domain_name_by_id = {}
    for raw in client.get_l2domains():
        raw_id = str(raw.get("id"))
        # Read as `sections`, written as `permissions`.
        listed = _section_ids(raw.get("sections", raw.get("permissions")))
        if raw_id != "1" and section_id not in listed:
            continue
        domain_name_by_id[raw_id] = raw.get("name") or ""
        fields, _ = model.partition_domain(raw)
        domains.append(fields)

    for raw in client.get_vlans():
        domain_id = str(raw.get("domainId") or "1")
        if domain_id not in domain_name_by_id:
            continue
        fields, _ = model.partition_vlan(raw)
        fields["domain"] = domain_name_by_id[domain_id]
        vlans.append(fields)
        vlan_ref_by_id[str(raw.get("id"))] = {
            "domain": domain_name_by_id[domain_id],
            "number": str(raw.get("number") or ""),
        }

    for raw in client.get_vrfs():
        if section_id not in _section_ids(raw.get("sections")):
            continue
        fields, _ = model.partition_vrf(raw)
        vrfs[str(raw.get("id"))] = fields
        vrf_name_by_id[str(raw.get("id"))] = raw.get("name") or ""

    return domains, vlans, list(vrfs.values()), vlan_ref_by_id, vrf_name_by_id


def _discover_custom_fields(getter, sample_id, kind):
    """Learns one table's custom-field names from a single-record read
    (list endpoints omit the nested block when all values are null).
    Returns an empty set on failure."""
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
    total_subnets = 0
    dropped_fields = {"subnet": set(), "address": set()}
    # Whether custom-field nesting is on; picks the dropped-field advice.
    saw_nested_custom_fields = False
    # Custom-field names per table; see _discover_custom_fields.
    custom_names = {"subnet": set(), "address": set()}

    all_sections = client.get_sections()
    by_name = {
        str(section.get("name", "")).strip().casefold(): section
        for section in all_sections
    }

    if config.get("all_sections"):
        section_names = sorted((str(s.get("name", "")).strip() for s in all_sections),
                               key=str.casefold)
        if not section_names:
            raise ConfigError("`sections: all`, but this API app can see no sections.")
        log(f"sections: all ({len(section_names)} visible to this API app)")
    else:
        section_names = config["sections"]

    for section_name in section_names:
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

        # VLANs/VRFs replicate whether or not a subnet references them.
        domains, vlans, vrfs, vlan_ref_by_id, vrf_name_by_id = _l2_context(
            client, section_id, options)
        # The section document is built after subnets (folders are rows).
        if vlans or vrfs:
            log(f"  {len(vlans)} vlan(s) in {len(domains)} l2 domain(s), "
                f"{len(vrfs)} vrf(s)")

        subnets = client.get_subnets_in_section(section_id)
        # Folders are subnets rows, listed first, and GET subnets/{id}/ on
        # one is 404, so the custom-field probe must skip them.
        real_subnets = [
            raw for raw in subnets
            if str(raw.get("isFolder") or "0") != "1"
        ]
        log(f"section {section_name!r} (id {section_id}): "
            f"{len(real_subnets)} subnet(s)")
        total_subnets += len(real_subnets)

        # With `all`, an empty section is ordinary; only an empty export fails.
        if not real_subnets and fail_on_empty and not config.get("all_sections"):
            raise ConfigError(
                f"Section {section_name!r} contains no subnets. If that is "
                f"genuinely correct, drop --fail-on-empty; otherwise check "
                f"the API app's permissions on this section."
            )

        if real_subnets and not custom_names["subnet"]:
            custom_names["subnet"] = _discover_custom_fields(
                client.get_subnet, real_subnets[0]["id"], "subnet")
            if custom_names["subnet"]:
                saw_nested_custom_fields = True
                log(f"  custom subnet field(s): "
                    f"{', '.join(sorted(custom_names['subnet']))}")

        # Folder paths: natural key for a subnet's parent folder.
        folder_paths = _folder_paths(subnets)
        if folder_paths:
            log(f"  {len(folder_paths)} folder(s)")

        documents.append(build_section_document(
            section_name=section_name, domains=domains,
            vlans=vlans, vrfs=vrfs, folders=folder_paths.values(),
        ))

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
            if str(raw.get("isFolder") or "0") == "1":
                continue  # replicated via the section document's folders
            if not raw.get("subnet") or raw.get("mask") in (None, ""):
                log(f"  skipping subnet id {raw.get('id')} -- no network/mask "
                    f"and not marked as a folder")
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
            parent_folder = folder_paths.get(str(master_id))
            if parent_folder:
                pass  # the parent is a folder, recorded by path below
            elif master_id and str(master_id) not in ("0", ""):
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
                parent_folder=parent_folder,
                vlan=vlan_ref_by_id.get(str(raw.get("vlanId") or "")),
                vrf=(
                    {"name": vrf_name_by_id[str(raw.get("vrfId"))]}
                    if str(raw.get("vrfId") or "") in vrf_name_by_id
                    else None
                ),
            ))
            log(f"  {cidr}: {len(addresses)} address(es)")

    if fail_on_empty and config.get("all_sections") and not total_subnets:
        raise ConfigError("`sections: all`, but no section holds any subnets: "
                          "check the API app's permissions.")

    source_side = config["source"]
    manifest = write_snapshot(
        out_dir,
        documents,
        source={
            "base_url": source_side.get("base_url"),
            "app_id": source_side.get("app_id"),
        },
        sections=section_names,
        dropped_fields={
            kind: sorted(names) for kind, names in dropped_fields.items() if names
        },
    )

    for kind, names in sorted(dropped_fields.items()):
        if names:
            log(f"note: {kind} field(s) present on the source but not "
                f"replicated: {', '.join(sorted(names))}")
            if not saw_nested_custom_fields:
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
    """Commits the snapshot if it changed, and pushes if asked."""
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
