#!/usr/bin/env python3
"""
ipam_import.py -- runs on Instance 2 (the replica).

Reads the snapshot delivered by the one-way git mirror and reconciles it
into the local phpIPAM instance over the REST API.

**Dry-run is the default.** Without `--apply` this script makes no write
of any kind; it prints exactly the plan that `--apply` would execute.
That default is deliberate -- the underlying client's write methods have
never been exercised against a live phpIPAM instance (see
phpipam_client.py's own docstring), so the first runs against a real
target should be read and checked by a human.

    # show what would change (writes nothing)
    ./ipam_import.py --config config.yml --snapshot-dir ../ipam-data

    # pull the latest mirrored snapshot, then show what would change
    ./ipam_import.py --config config.yml --snapshot-dir ../ipam-data --pull

    # actually reconcile (the normal cron invocation, once trusted)
    ./ipam_import.py --config config.yml --snapshot-dir ../ipam-data \\
        --pull --apply

## Additive by default; strict mirror on request

By default this importer never deletes. Records present on the target but
absent from the snapshot are reported as `drift_*` lines and left alone.
`--quiet-drift` suppresses that report once you have accepted that the
target carries extra records.

`--delete` (or `options.delete_drift: true`) turns those reports into
real deletions, making the replica a strict mirror:

    # see what a strict-mirror run WOULD delete -- writes nothing
    ./ipam_import.py --config config.yml --snapshot-dir ../ipam-data --delete

    # strict mirror, for real
    ./ipam_import.py --config config.yml --snapshot-dir ../ipam-data \\
        --pull --delete --apply

Deletion is guarded three ways: it still requires `--apply`; it is scoped
to the replicated sections exactly like every other action; and a safety
limit refuses the run outright if it would delete more than
`options.delete_limit_fraction` (default 10%) of the in-scope records.
That last one is the important one -- an empty or mis-scoped snapshot
arriving over a link the replica cannot question should not be able to
take the whole replica with it. `--force-delete` overrides it for one
run, and `--no-delete` forces additive-only regardless of config.

Exit status: 0 = success (plan clean, or applied without error);
1 = the run could not proceed (bad config, corrupt snapshot, missing
section, or the delete safety limit tripped); 2 = applied, but one or
more individual records failed.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipamsync.config import (                                     # noqa: E402
    ConfigError, build_client, load_config, resolve_sources,
)
from ipamsync.plan import (                                          # noqa: E402
    PlanError, build_plan, canonicalise_document_cidr, summarise,
)
from ipamsync.snapshot import (                                      # noqa: E402
    SnapshotError, find_stale_files, read_snapshot,
)
from ipamsync.target import Executor, TargetError, TargetView        # noqa: E402


def log(message):
    print(message, flush=True)


def git_pull(snapshot_dir):
    """Fast-forwards the mirrored repo before reading it.

    The mirror is one-way, so this side never has local commits to
    reconcile -- a fast-forward is the only correct outcome, and
    --ff-only turns "someone committed on the replica side" into a loud
    failure instead of a merge commit that would then diverge from the
    source forever.
    """
    completed = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=snapshot_dir, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git pull --ff-only failed in {snapshot_dir}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}\n"
            f"If this replica has local commits in the mirrored repo, reset "
            f"it to the remote -- this side must never diverge."
        )
    log(completed.stdout.strip() or "already up to date")


def print_plan(actions, quiet_drift=False, label=""):
    writes = [action for action in actions if action.is_write]
    deletes = [action for action in writes if action.kind.startswith("delete_")]
    other = [action for action in writes if not action.kind.startswith("delete_")]
    notes = [action for action in actions if action.kind == "note"]
    drift = [action for action in actions
             if action.kind in ("drift_subnet", "drift_address")]

    if other:
        log(f"\n{label}changes:")
        for action in other:
            log(f"  {action.describe()}")
    elif not deletes:
        log(f"\n{label}changes: none -- target already matches the snapshot")

    # Deletions get their own block, always shown in full and never
    # folded in with creates and updates. They are the only thing here
    # that destroys data, so they should be the first thing read.
    if deletes:
        log(f"\n{label}DELETIONS ({len(deletes)}) -- records on the target that the "
            f"snapshot no longer contains:")
        for action in deletes:
            log(f"  {action.describe()}")

    if notes:
        log(f"\n{label}notes:")
        for action in notes:
            log(f"  {action.describe()}")

    if drift and not quiet_drift:
        log(f"\n{label}drift ({len(drift)} record(s) on the target but not in the "
            f"snapshot -- reported only, never deleted):")
        for action in drift:
            log(f"  {action.describe()}")

    counts = summarise(actions)
    log(f"\n{label}summary: " + (", ".join(
        f"{kind}={count}" for kind, count in sorted(counts.items())
    ) or "nothing to do"))
    return writes


def snapshot_age_days(manifest):
    """Days since the snapshot's content last changed, or None if the
    timestamp is unreadable.

    Note this is content age, not "when the exporter last ran" -- the
    exporter deliberately does not rewrite an unchanged snapshot. A site
    whose networks genuinely have not changed for weeks will look old.
    It is still the only staleness signal that crosses the one-way link,
    and on a master aggregating several subordinates it is the difference
    between "site D is quiet" and "site D's mirror died a month ago".
    """
    stamp = manifest.get("exported_at")
    if not stamp:
        return None
    try:
        exported = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - exported).total_seconds() / 86400


def run_one_source(name, config, snapshot_dir, args, client, view):
    """Imports one snapshot. Returns (write_count, error_count, ok).

    `ok` is False when this source could not be processed at all (bad or
    missing snapshot, unresolvable section). On a master, that must not
    stop the other subordinates: one site's mirror being broken is not a
    reason to leave the other five unsynced.
    """
    label = f"[{name}] " if name else ""
    try:
        try:
            manifest, documents = read_snapshot(snapshot_dir)
        except SnapshotError as exc:
            if args.allow_empty and "lists no subnet files" in str(exc):
                log(f"{label}snapshot is empty; --allow-empty given, skipping")
                return 0, 0, True
            raise

        age = snapshot_age_days(manifest)
        age_text = f", content {age:.1f}d old" if age is not None else ""
        log(f"{label}snapshot {manifest['exported_at']}{age_text}: "
            f"{manifest['subnet_count']} subnet(s), "
            f"{manifest['address_count']} address(es)")

        limit = args.stale_after_days
        if limit and age is not None and age > limit:
            log(f"{label}WARNING: this snapshot has not changed in "
                f"{age:.0f} days (limit {limit}). Either that subordinate's "
                f"networks are genuinely static, or its export/mirror has "
                f"stopped. Check the export job on that site.")

        stale = find_stale_files(snapshot_dir, manifest)
        if stale:
            log(f"{label}warning: {len(stale)} file(s) not listed in the "
                f"manifest will be ignored: {', '.join(stale[:5])}"
                + (" ..." if len(stale) > 5 else ""))

        for kind, names in sorted((manifest.get("dropped_source_fields") or {}).items()):
            if names:
                log(f"{label}note: exporter did not carry these {kind} "
                    f"field(s): {', '.join(names)}")

        documents = [canonicalise_document_cidr(d) for d in documents]
        actions = build_plan(documents, view, config)
        writes = print_plan(actions, quiet_drift=args.quiet_drift, label=label)

        if not args.apply or not writes:
            return len(writes), 0, True

        log(f"\n{label}applying {len(writes)} change(s)...")
        executor = Executor(client, view, config, log)
        applied, errors = executor.apply(actions)
        log(f"{label}applied {applied} change(s), {len(errors)} error(s)")
        if errors:
            for error in errors:
                log(f"{label}  {error}")
        return applied, len(errors), True

    except (SnapshotError, PlanError, TargetError, ConfigError) as exc:
        log(f"{label}FAILED: {exc}")
        return 0, 0, False


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--snapshot-dir", default=None,
                        help="Snapshot root inside the working copy of the "
                             "mirrored git repo. Omit when the config has a "
                             "`sources:` list (master/fan-in), which carries "
                             "a path per subordinate.")
    parser.add_argument("--pull", action="store_true",
                        help="git pull --ff-only the mirrored repo first.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to the target instance. Without "
                             "this, the run is strictly read-only.")
    parser.add_argument("--quiet-drift", action="store_true",
                        help="Suppress the drift report.")
    parser.add_argument("--delete", dest="delete", action="store_true",
                        default=None,
                        help="Delete target records the snapshot no longer "
                             "contains, making this a strict mirror. "
                             "Overrides options.delete_drift for this run. "
                             "Still requires --apply.")
    parser.add_argument("--no-delete", dest="delete", action="store_false",
                        help="Force additive-only for this run, even if "
                             "options.delete_drift is set in the config.")
    parser.add_argument("--force-delete", action="store_true",
                        help="Bypass the deletion safety limit "
                             "(options.delete_limit_fraction) for this run. "
                             "Only after checking WHY the run wants to "
                             "delete that much -- the usual cause is a bad "
                             "snapshot, not a real bulk deletion upstream.")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Accept a snapshot that lists no subnets, "
                             "instead of treating it as a failed export.")
    parser.add_argument("--source", default=None,
                        help="With a master (fan-in) config, process only "
                             "this named source instead of all of them.")
    parser.add_argument("--stale-after-days", type=float, default=30,
                        help="Warn when a snapshot's content has not changed "
                             "in this many days -- on a master, the signal "
                             "that a subordinate's mirror has stopped. "
                             "0 disables. Default 30.")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        if args.delete is not None:
            config["options"]["delete_drift"] = args.delete
        if args.force_delete:
            config["options"]["force_delete"] = True
        deleting = bool(config["options"].get("delete_drift"))

        sources = resolve_sources(config, only=args.source)
        if not sources:
            if args.source:
                raise ConfigError(
                    "--source given, but this config has no `sources:` list. "
                    "It looks like a single-source config."
                )
            if not args.snapshot_dir:
                raise ConfigError(
                    "Need --snapshot-dir (single-source config) or a "
                    "`sources:` list in the config (master/fan-in)."
                )
            sources = [(None, {**config, "snapshot_dir": args.snapshot_dir})]

        log(f"mode: {'STRICT MIRROR (deletes)' if deleting else 'additive (no deletes)'}"
            f" | {len(sources)} source(s)")

        # One client and one cached view for the whole run. The view is
        # shared deliberately: sources own disjoint sections, so their
        # reads never overlap, and sharing avoids re-fetching the target's
        # section list once per subordinate.
        client = build_client(config, "target")
        view = TargetView(client)

        total_writes = total_errors = 0
        failed_sources = []

        for name, source_config in sources:
            snapshot_dir = source_config["snapshot_dir"]
            if args.pull:
                try:
                    git_pull(snapshot_dir)
                except Exception as exc:  # noqa: BLE001
                    # A broken mirror for one site must not stop the rest.
                    log(f"[{name}] FAILED: {exc}" if name else f"FAILED: {exc}")
                    failed_sources.append(name or snapshot_dir)
                    continue

            writes, errors, ok = run_one_source(
                name, source_config, snapshot_dir, args, client, view,
            )
            total_writes += writes
            total_errors += errors
            if not ok:
                failed_sources.append(name or snapshot_dir)

        if len(sources) > 1 or failed_sources:
            log(f"\n== {len(sources) - len(failed_sources)}/{len(sources)} "
                f"source(s) processed, {total_writes} change(s), "
                f"{total_errors} record error(s)")
            if failed_sources:
                log(f"   sources that could not be processed: "
                    f"{', '.join(str(s) for s in failed_sources)}")

        if not args.apply:
            if total_writes:
                log(f"\ndry run -- nothing was written. Re-run with --apply "
                    f"to make these {total_writes} change(s).")
            return 1 if failed_sources else 0

        if failed_sources:
            return 1
        return 2 if total_errors else 0

    except (ConfigError, SnapshotError, PlanError, TargetError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
