# phpipam-sync

One-way replication of phpIPAM subnets and addresses between independent
instances, using a git repository as the only transport.

A **source** owns its networks; a **target** holds a copy that is never
written back. Neither side needs to reach the other, so it works across
an air gap.

```
  source phpIPAM            git repo (mirrored one-way)        target phpIPAM
  ┌──────────┐   export     ┌────────────────────────┐  import   ┌──────────┐
  │ phpIPAM  │ ──────────▶  │ manifest.json          │ ────────▶ │ phpIPAM  │
  │ (owns    │   read-only  │ sections/shared/*.json │   apply   │ (copy)   │
  │  the     │              └────────────────────────┘           └──────────┘
  │  data)   │
  └──────────┘
```

- **`ipam_export.py`** runs on the source. Reads the configured
  section(s) over the REST API and writes a canonical JSON snapshot into
  the mirrored repo. **Never writes to phpIPAM.**
- **`ipam_import.py`** runs on the target. Reconciles the snapshot(s)
  into the local instance. **Dry-run by default; additive unless you ask
  for a strict mirror.**

Other documents:

- [DEPLOYMENT.md](DEPLOYMENT.md): setup procedure, cron, monitoring,
  runbook, checklists.
- [GIT-EXPORT.md](GIT-EXPORT.md): export only, as a git history/backup of
  phpIPAM (one data repo per instance, this repo as its `tool/`
  submodule).
- [FILL.md](FILL.md): one-off seed of empty sources from the master, one
  section per instance, before the normal sync starts.

**Contents**

- [Status](#status)
- [How it works](#how-it-works)
- [What is and isn't replicated](#what-is-and-isnt-replicated)
- [Topologies](#topologies)
- [Usage](#usage)
- [Operating it](#operating-it)
  - [Scale and cost per run](#scale-and-cost-per-run)
  - [Recovering from a mirror outage](#recovering-from-a-mirror-outage)
  - [Repository growth](#repository-growth)
  - [Making the target read-only](#making-the-target-read-only)
  - [Strict mirror and drift](#strict-mirror-and-drift)
  - [Monitoring](#monitoring)
- [Trust model](#trust-model)
- [phpIPAM API quirks](#phpipam-api-quirks)
- [Testing](#testing)
- [Layout](#layout)
- [License](#license)

---

## Status

Targets **phpIPAM 1.8.1 and 1.9.1**. Tested end to end against real
instances of both, with independent databases and diverged id counters;
all lab checks pass on each, and 1.9.1 returns no subnet or address
fields the tool does not handle.

`lab/` builds three throwaway instances in Docker:

```bash
cd lab && ./setup.sh          # or: PHPIPAM_VERSION=v1.9.1 ./setup.sh
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
export PHPIPAM_DST_TOKEN=DSTTOKEN0000000000000000000000
./verify.sh           # 1:1 replication        -- 82 assertions
./verify-fanin.sh     # many-to-one fan-in     -- 22 assertions
./verify-catchup.sh   # mirror-outage recovery -- 14 assertions
docker compose down
```

Plus **193 unit/integration tests** needing no network, phpIPAM, or
credentials. CI runs them on every push.

### Other versions

Other versions are unverified: seven behaviours this tool depends on are
undocumented API quirks (see [phpIPAM API quirks](#phpipam-api-quirks)).
To qualify a version, run the lab against it
(`PHPIPAM_VERSION=<tag>`, see [lab/README.md](lab/README.md)).

### First contact with your own instances

1. Run the importer **without `--apply`**: it writes nothing and prints
   the plan.
2. Read the exporter's `dropped_source_fields` report: every field the
   source returned that this tool did not carry.
3. Apply once against a **throwaway section**.
4. Then wire it into cron.

Not covered: instances with custom `ipTags` (see `options.sync_tags`),
and phpIPAM's permissions model beyond what is described below.

---

## How it works

### State, not events

The importer reads the snapshot **tree at `HEAD`**, never the commit
history, compares it with the target, and writes only the difference. A
steady-state run writes nothing; missed commits collapse into one
reconciliation; a record created and deleted while the mirror was down
never reaches the target.

### Records are matched by natural key, never by id

The two instances share no database ids. `id`, `sectionId`, `vlanId`,
`masterSubnetId`, `gatewayId`, `deviceId` index *that instance's* tables,
so nothing is matched or carried by id:

| Thing | Natural key |
|---|---|
| Section | name (case-insensitive) |
| Subnet | (target section, canonical CIDR) |
| Address | (subnet, IP) |

Nested subnets are recorded by the **parent's CIDR** and re-nested on the
target by looking that CIDR up locally.

The field policy in `ipamsync/model.py` is an allowlist of literal
values, with the reason each excluded field is excluded. `gatewayId` is
excluded; the gateway replicates via `is_gateway` on the address and the
target recomputes its own.

### The snapshot format

```
manifest.json
sections/shared/10.20.5.0_24.json
```

An annotated example is in [`examples/`](examples/), verified by the
test suite.

- **One file per subnet**, holding its fields and all its addresses.
- **Canonically serialised**: sorted keys, fixed indent, addresses in
  numeric IP order, trailing newline. An unchanged export is
  byte-identical, so the exporter commits only on real change.
- **Every value is a string.** The API returns the same field as `1`,
  `"1"` or `true` depending on version and endpoint; values are
  normalised so a steady-state sync finds no false differences.
- **`manifest.json` checksums every file.** The importer verifies all of
  them before touching the target, so a truncated transfer fails rather
  than applies. Files the manifest does not list are ignored. Digests
  prove integrity, not authorship; see [Trust model](#trust-model).

### Additive by default

The importer deletes nothing unless told to. Target records absent from
the snapshot are reported as **drift** and left alone. See
[Strict mirror and drift](#strict-mirror-and-drift).

---

## What is and isn't replicated

**Replicated:** subnet definitions (CIDR, description, literal
display/scan flags), nesting between in-scope subnets, and every address
inside them (hostname, description, MAC, owner, note, port, gateway
flag, ping/PTR flags), plus **custom fields** on both. IPv4 and IPv6.

**Also replicated: folders, L2 domains, VLANs and VRFs**, and each
subnet's link to them. Each is carried as a record in its own right, so
one attached to nothing still replicates.

A phpIPAM folder is a `subnets` row with `isFolder=1`, no network, and
its name in `description`. Folders replicate by **path** (a list of
names, since a name may contain a slash); a subnet in a folder lands in
the same folder on the target.

Natural keys: folder by **path**, L2 domain by **name**, VLAN by
**(domain, number)**, VRF by **(section, name)**. phpIPAM has no unique
index on any of them, so the same VLAN number in two domains, or the
same VRF name in two sections, are separate records.

Scoping follows phpIPAM: a VLAN belongs to a section through its L2
domain (`vlanDomains.permissions`, which holds *section ids* despite the
name), a VRF through its own `sections` list. On the target both lists
are only ever added to, never replaced, so one source's import cannot
evict another's.

**Not replicated:** devices, locations, nameservers, permissions,
customers.

**Deletions are not replicated by default**; see
[Strict mirror and drift](#strict-mirror-and-drift).

### Custom fields need "Nest custom fields" enabled

A phpIPAM custom field is a plain column named whatever was typed
("Owner" is column `Owner`). Only with **Nest custom fields = Yes** on
the API app does phpIPAM report them distinctly (under `custom_fields`),
and only then can they be replicated. That is a read format only: writes
must be flat, which is how the snapshot stores them.

Enable it on **both** sides. Off on the source: custom fields are
silently not carried. Mismatched: every custom field is rewritten on
every run.

**Custom fields are schema, not data.** This tool replicates values; it
cannot create fields. Every custom field a source uses must exist on the
target with the same name, or each record carrying it is rejected with
`Invalid request key <name>`.

A field not carried shows in `dropped_source_fields` in the manifest. To
carry one, add it to `SUBNET_SYNCED_FIELDS` or `ADDRESS_SYNCED_FIELDS`,
provided it is a literal value and not an id.

---

## Topologies

### 1:1 replication

One source, one replica. Configure both hosts from `config.example.yml`.

### Fan-in: many subordinates, one master

Several airgapped instances, each owning non-overlapping networks,
pushing into one read-only master. Subordinates are configured as for
1:1; the master uses `config.master.example.yml`.

```
  site-a  ──(site-a-ipam.git, mirrored)──┐
  site-b  ──(site-b-ipam.git, mirrored)──┤
  site-c  ──(site-c-ipam.git, mirrored)──┼──▶  MASTER phpIPAM
  site-d  ──(site-d-ipam.git, mirrored)──┤     (read-only to users)
  site-e  ──(site-e-ipam.git, mirrored)──┘
```

```bash
./ipam_import.py --config config.master.yml --pull            # dry run, all sites
./ipam_import.py --config config.master.yml --pull --apply    # for real
./ipam_import.py --config config.master.yml --source site-c   # just one
```

#### Each subordinate MUST own its own section on the master

Each import treats anything in its target section that is absent from
its snapshot as drift, and **deletes** it once `delete_drift` is on. Two
subordinates sharing a section would delete each other's subnets on
every run.

```yaml
sources:
  - name: site-a
    snapshot_dir: /srv/ipam/site-a-ipam
    sections: [Networks]
    section_map: {Networks: Site-A}
  - name: site-b
    snapshot_dir: /srv/ipam/site-b-ipam
    sections: [Networks]
    section_map: {Networks: Site-B}
```

The config loader refuses two sources targeting the same section
(case-insensitive, counting unmapped names), duplicate source names, and
two sources reading the same directory.

#### Other fan-in behaviour

- **One broken subordinate does not stop the others.** A stale mirror,
  corrupt snapshot or missing section is reported and skipped. Exit 1 if
  any source failed, 2 if individual records did.
- **Output is labelled per source** (`[site-a] ...`).
- **Staleness is reported per source**; see [Monitoring](#monitoring).

---

## Usage

Python 3.9+; the tool refuses to start on anything older. `requests` and
`PyYAML` are used if installed, otherwise the pure-Python copies in
`ipamsync/_vendor`, so an air-gapped host needs no pip. Under cron, name
the interpreter by absolute path; cron's PATH is not your shell's.

```bash
pip install -r requirements.txt
cp config.example.yml config.yml     # then edit
```

Each instance needs a phpIPAM **API app** (Administration → API) in
app-code / static-token mode, which **requires HTTPS**: over plain HTTP
phpIPAM returns `503 "SSL connection is required for API"`. Source app:
Read. Target app: Read/Write.

Tokens are never stored in `config.yml`. Each side names an environment
variable (`token_env`) or a command whose stdout is the token
(`token_command`: ansible-vault, pass, gpg, ...).

**On the source:**

```bash
./ipam_export.py --config config.yml --out-dir /srv/ipam-data --commit --push
```

`--out-dir` is a path inside a working copy of the mirrored repo. It
commits only if something changed.

**On the target:**

```bash
# dry run -- writes nothing, prints the plan
./ipam_import.py --config config.yml --snapshot-dir /srv/ipam-data --pull

# once trusted
./ipam_import.py --config config.yml --snapshot-dir /srv/ipam-data --pull --apply
```

`--pull` uses `--ff-only`: a local commit on the receiving side fails
the pull instead of merging.

**Exit codes:** `0` success · `1` could not proceed (bad config, corrupt
snapshot, missing section, a source failed, delete limit tripped) · `2`
applied, but individual records failed.

Wrap both in `flock`; there is no internal locking.

---

## Operating it

### Scale and cost per run

**Writes are diff-only; reads are not.** Every run reads the whole
dataset on both sides (one API call per subnet for its addresses), then
writes only what differs.

Lab stack (1.8.1, **204 subnets / 40,005 addresses**, one host, no
network latency):

| Operation | Time |
|---|---|
| Export (full read + snapshot + checksums) | ~7 s |
| Import, steady state — nothing changed | ~6 s |
| Import, incremental — 6 records changed | ~6 s |
| **Initial load** — 40,209 records created | **~16 min** (~43 writes/s) |

- **Routine syncs:** ~13 s per cycle for 40k records regardless of how
  much changed; a 5-minute cron is fine.
- **First load:** one POST per record. Run it by hand.

Cost scales with the **number of subnets** (`2 + N` calls per side), not
addresses. Add round-trip time × call count for a remote instance.

### Recovering from a mirror outage

The importer is state-based, so a mirror down for weeks does **not**
replay. `lab/verify-catchup.sh`: 14 days down, 50 address changes a day,
2,000-record estate:

| | |
|---|---|
| Backlog when the mirror returned | 14 commits |
| Edits made upstream | 700 |
| Runs needed to converge | **1** |
| Time to converge | **17 s** |
| Changes applied | 700 — bounded by the *net* diff |
| Created *and* deleted mid-outage | never appeared, not even in the plan |
| Immediate re-run | no-op |

A host renamed five times during the outage is written once. Worst case
(every record changed) equals a full reload: ~16 min per 40,000 records.

With `delete_drift` on, an outage that coincided with mass deletion
upstream trips the safety limit and **the whole run is refused**, target
untouched. Procedure in
[DEPLOYMENT.md](DEPLOYMENT.md#when-the-mirror-fails-and-then-catches-up).

### Repository growth

`lab/measure-growth.py`, one year of churn on a 40,000-record estate:

| Churn | After 365 days | After `git gc` |
|---|---|---|
| 30 address changes/day | 27.5 MB | **2.6 MB** |
| 300 address changes/day | 38.4 MB | **12.2 MB** |

Low-churn case: 371 MB of uncompressed content packed to 2.3 MB. Ten
years of heavy churn: ~120 MB.

No routine cleanup needed; the larger figures are loose objects awaiting
git's housekeeping. The master can clone shallow (592 KB vs 2.7 MB).
Maintenance and history truncation:
[DEPLOYMENT.md](DEPLOYMENT.md#repository-growth-and-maintenance).

Keeps it small (do not break): commit only on real change, canonical
serialisation, one file per subnet.

### Making the target read-only

phpIPAM group permissions make mirrored subnets read-only to users
without affecting the sync:

- **Group permissions do not gate the API app**; only the app's
  `app_permissions` does.
- **The importer never touches `permissions`**, so what you set survives
  re-syncs.
- **New subnets inherit their parent's permissions.**

**Gotcha:** a nested subnet inherits from its **parent subnet**, not the
section. With the section at `{"2":"1","3":"1"}` and a parent subnet at
`{"2":"3"}`, a newly replicated child came out `{"2":"3"}`. Set the
permission on **the section and every existing top-level subnet** in it.

Administrators bypass all of this; see
[DEPLOYMENT.md § 2.5b](DEPLOYMENT.md#25b-read-only-to-administrators-as-well-optional).

### Strict mirror and drift

With deletion off, the target accumulates records the source no longer
has, plus anything created locally. Every run reports them:

```
drift_address  10.20.5.0/24 10.20.5.10  -- exists on target, absent from
                                           snapshot (not deleted --
                                           deletion is disabled)
```

`--quiet-drift` suppresses the report. With deletion off the target
progressively **over-reports utilisation**.

To track deletions:

```bash
./ipam_import.py --config config.yml --snapshot-dir /srv/ipam-data --delete
./ipam_import.py --config config.yml --snapshot-dir /srv/ipam-data \
    --pull --delete --apply
```

```yaml
options:
  delete_drift: true          # standing policy; --no-delete overrides per run
  delete_limit_fraction: 0.1  # refuse a run deleting >10% of in-scope records
```

Guards:

1. **`--apply` is still required**; deletions print in their own
   `DELETIONS` block.
2. **Scope is unchanged**: only configured sections.
3. **Safety limit**: a run deleting more than `delete_limit_fraction` of
   in-scope records is refused (floor of 5 records). Exit 1, nothing
   written:

```
Refusing to run: this would delete 61 of 69 in-scope record(s) on the
target (88%), which is over the safety limit of 6 (10%, floor 5).
This usually means the snapshot is empty, truncated, or was exported
from the wrong section -- NOT that this much really was deleted
upstream. Check the source instance and the snapshot's manifest.json
first.
```

`--force-delete` overrides for one run; check the cause first.

Deletion order: addresses, then subnets deepest-first. Strict mirror
also removes anything created by hand on the target. What deletion
covers for folders, VLANs and VRFs:
[DEPLOYMENT.md Part 5](DEPLOYMENT.md#part-5--switching-to-strict-mirror-later).

### Monitoring

**The target cannot detect that a source has stopped exporting.** The
exporter does not rewrite an unchanged snapshot, so `manifest.json`'s
`exported_at` means *content last changed*, not *the exporter ran*.

1. **Monitor the export job on each source** (cron exit status).
2. **`--stale-after-days`** (default 30) warns when a snapshot's content
   has not changed in that long. A static site trips it too.

Details: [DEPLOYMENT.md](DEPLOYMENT.md#monitoring).

---

## Trust model

**The master trusts every subordinate that can write to a mirrored
repo.** A snapshot is a set of writes the importer carries out.

- **The mirrored repo is the security boundary.** Anyone who can commit
  to a subordinate's repo can add or update records in that
  subordinate's section on the master, and with `options.delete_drift`
  on, delete them. Protect write access to those repos like write access
  to the master's database. Master-side deploy keys should be read-only.
- **Checksums are integrity, not authenticity.** A self-consistent
  manifest is easy to author. To verify origin, sign commits on the
  subordinate and check `git verify-commit HEAD` before importing; the
  importer does not do this.
- **The importer will not read outside its snapshot directory.** A
  manifest naming `../../etc/passwd`, an absolute path, or a symlink out
  of the tree is refused even when its digest matches
  (`_safe_relative_path`, tested in `tests/test_snapshot.py`).
- **Each source is confined to its own section**; the loader refuses two
  sources targeting one section (`_validate_sources` in
  `ipamsync/config.py`).
- **`token_command` runs a command**, so whoever can edit `config.yml`
  can execute code as the sync user.
- **Blast radius is bounded by the API app's rights.** Exporter app:
  `Read`. Importer app: `Read/Write`. Additive mode (default) limits the
  worst case to wrong records added or updated.
- **"Read-only master" means read-only to users, not admins.** phpIPAM
  grants an Administrator full rights before consulting any permission.
  Enforcing it for admins needs database grants and a second phpIPAM
  configuration; see
  [DEPLOYMENT.md § 2.5b](DEPLOYMENT.md#25b-read-only-to-administrators-as-well-optional).

Tokens are never read from the config file, are sent as headers rather
than on the command line, and are kept out of error messages (a failing
`token_command` reports its stderr, never its stdout).

---

## phpIPAM API quirks

Undocumented 1.8.1 behaviour this tool handles. Requests and responses
for each are in [`lab/README.md`](lab/README.md).

1. **Empty collections are HTTP 404**, not an empty list (supernets with
   no addresses; the first import into an empty section).
2. **Subnets are read as `tag` but written as `state`.**
3. **Custom fields have no distinguishing name**; only "Nest custom
   fields" separates them.
4. **Custom fields come back nested from the target too**; compared
   unflattened, every run rewrites every custom field.
5. **Overlap is validated on create but not on update.** A create that
   overlaps a sibling gets `409`; `PATCH masterSubnetId` accepts
   anything. Re-parenting detaches before it creates.
6. **A folder is a subnet row the subnet endpoint will not serve**
   (`GET subnets/{id}/` on a folder is `404`), and folders are listed
   *first*.
7. **Deleting a folder deletes everything inside it**, reporting only
   `Subnet deleted`. Deleting a VLAN or VRF nulls the subnets' reference
   instead. Folders are only removed once confirmed empty.

---

## Testing

```bash
python3 -m unittest discover -s tests -v          # 193 tests, no network
```

Every "these compare equal" assertion is paired with a negative control
proving a real difference is still detected.

- `tests/test_end_to_end.py`: real exporter and importer against an
  in-memory fake reproducing phpIPAM's quirks. Headline assertion:
  `test_second_import_is_a_no_op`.
- `tests/test_http_roundtrip.py`: the full stack over a real socket.
- `tests/test_examples.py`: verifies the committed example snapshot.
- `lab/`: everything against real phpIPAM.

Each of these mutations fails the suite: neutering the field diff,
removing checksum verification, carrying a source id across, renaming
the auth header, reverting the `tag`→`state` alias, disabling
custom-field discovery, removing the delete safety limit, removing the
fan-in section-collision guard.

---

## Layout

| Path | What |
|------|------|
| `ipam_export.py` | Source entry point |
| `ipam_import.py` | Target entry point (single-source or fan-in) |
| `ipamsync/model.py` | Field policy: what crosses instances, and why |
| `ipamsync/snapshot.py` | On-disk format, canonical serialisation, checksums |
| `ipamsync/plan.py` | Snapshot + target state → ordered action list |
| `ipamsync/target.py` | Cached target reads; plan execution |
| `ipamsync/config.py` | Config loading, token resolution, fan-in validation |
| `ipamsync/client_ext.py` | Endpoints and quirks the base client lacks |
| `phpipam_client.py` | Generic phpIPAM API client — no replication logic |
| `config.example.yml` | Source / 1:1 configuration |
| `config.master.example.yml` | Master (fan-in) configuration |
| `GIT-EXPORT.md` | Using the exporter as a git history of phpIPAM |
| `contrib/export.sh` | Cron wrapper for a data repo (`tool/` submodule layout) |
| `contrib/ansible/` | Playbook running the export with tokens from a vault |
| `contrib/data-repo/` | Config and .gitignore for a new data repo |
| `ipamsync/_vendor/` | requests + PyYAML, pure Python, used when not installed |
| `DEPLOYMENT.md` | Step-by-step setup, cron, monitoring, runbook |
| `FILL.md` | One-off seeding of the sources from the master, per section |
| `examples/` | A real snapshot, annotated — verified by the tests |
| `lab/` | Three phpIPAM instances in Docker + `verify.sh` (1:1), `verify-fanin.sh`, `verify-catchup.sh`, `measure-growth.py` |

`phpipam_client.py` is a standalone client with nothing sync-specific;
`client_ext.py` subclasses it for empty-collection handling and section
creation.

---

## License

MIT. See [LICENSE](LICENSE).

This project is not affiliated with the phpIPAM project. It talks to
phpIPAM's public REST API and ships no phpIPAM code.
