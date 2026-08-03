# phpipam-sync

One-way replication of phpIPAM subnets and addresses between independent
instances, using a git repository as the only transport.

Data flows in one direction: a **source** owns its networks, and a
**target** holds a copy that is never written back. Nothing assumes the
source can be reached from the target, or vice versa — which is what
makes it work across an air gap.

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
- **`ipam_import.py`** runs on the target. Reads the snapshot(s) and
  reconciles them into the local instance. **Dry-run by default; additive
  unless you ask for a strict mirror.**

📘 **Deploying this? Start with [DEPLOYMENT.md](DEPLOYMENT.md)** — the
step-by-step procedure, cron, monitoring, and a runbook of every error
this can produce. This README is the *what and why*; that is the *how*.

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
  - [Monitoring, and what the repo cannot tell you](#monitoring-and-what-the-repo-cannot-tell-you)
- [Trust model](#trust-model)
- [What real phpIPAM taught us](#what-real-phpipam-taught-us)
- [Testing](#testing)
- [Layout](#layout)
- [License](#license)

---

## Status

Run end to end against **real phpIPAM instances** with independent
databases and deliberately diverged id counters. Verified on **1.8.1**
(primary) and **1.7.4**. Subnet and address field sets are identical
between those two versions.

`lab/` builds three throwaway instances in Docker so the result is
reproducible rather than a claim:

```bash
cd lab && ./setup.sh
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
export PHPIPAM_DST_TOKEN=DSTTOKEN0000000000000000000000
./verify.sh           # 1:1 replication        -- 75 assertions
./verify-fanin.sh     # many-to-one fan-in     -- 22 assertions
./verify-catchup.sh   # mirror-outage recovery -- 14 assertions
docker compose down

PHPIPAM_VERSION=v1.7.4 ./verify.sh          # or any published tag
```

Plus **172 unit/integration tests** needing no network, phpIPAM, or
credentials. CI runs them on every push.

### What is not proven

The lab covers 1.8.1 and 1.7.4. Other versions may differ in exactly the
ways that produced the bugs below. On first contact with your own
instances:

1. Run the importer **without `--apply`** — it writes nothing and prints
   the exact plan.
2. Read the exporter's `dropped_source_fields` report. It names every
   field your source returned that this tool did not carry — the fastest
   way to spot a version difference.
3. Apply once against a **throwaway section**.
4. Only then wire it into cron.

Not covered: instances with custom `ipTags` (see `options.sync_tags`),
and phpIPAM's permissions model beyond what is described below.

---

## How it works

### State, not events

The importer reads the snapshot **tree at `HEAD`** and never looks at
commit history. It compares that desired state against the target's
current state and writes only the difference.

Everything else follows from this. A steady-state run writes nothing. A
fortnight of missed commits collapses into one reconciliation. A record
created and deleted while the mirror was down never reaches the target at
all, because it is not in the final tree.

### Records are matched by natural key, never by id

The two instances share no database ids. Every phpIPAM `id`, `sectionId`,
`vlanId`, `masterSubnetId`, `gatewayId`, `deviceId` is an integer
indexing *that instance's* other tables — copying one across points a
record at an unrelated row. So nothing is matched or carried by id:

| Thing | Natural key |
|---|---|
| Section | name (case-insensitive) |
| Subnet | (target section, canonical CIDR) |
| Address | (subnet, IP) |

Nested subnets are recorded by the **parent's CIDR** and re-nested on the
target by looking that CIDR up locally. Verified live: the replica's
`masterSubnetId` is its own id for the parent, not the source's.

The field policy in `ipamsync/model.py` is a conservative allowlist of
literal values, and documents why each excluded field is excluded. That
default-deny stance earned itself: `gatewayId` — an id into the source's
own address table — was not in any documentation-derived list, and the
allowlist kept it out anyway. The gateway still replicates, via
`is_gateway` on the address, and the target recomputes its own.

### The snapshot format

```
manifest.json
sections/shared/10.20.5.0_24.json
```

A complete, annotated example lives in [`examples/`](examples/) — real
exporter output, verified by the test suite on every run so it cannot
drift from the code.

- **One file per subnet**, holding its fields and all its addresses. The
  git history then shows *which* subnet changed, and two subnets changing
  independently produce non-overlapping diffs.
- **Canonically serialised** — sorted keys, fixed indent, addresses in
  numeric IP order, trailing newline. An unchanged export is byte-
  identical, which is what lets the exporter commit only on real change.
- **Every value is a string.** phpIPAM's API is loosely typed — the same
  field arrives as `1`, `"1"` or `true` depending on version and endpoint
  — so values are normalised. Without this a steady-state sync would
  "find" differences that do not exist.
- **`manifest.json` checksums every file.** The importer verifies all of
  them before touching the target: a one-way link gives the receiving
  side no way to ask for a resend, so a truncated transfer must fail
  loudly rather than apply as if complete. Files the manifest does not
  list are treated as stale and ignored. These digests prove the tree
  arrived **intact**, not who wrote it — see [Trust model](#trust-model).

### Additive by default

The importer never deletes unless told to. Records on the target that the
snapshot does not contain are reported as **drift** and left alone, so
the worst a bad snapshot can do is add or update. See
[Strict mirror and drift](#strict-mirror-and-drift).

---

## What is and isn't replicated

**Replicated:** subnet definitions (CIDR, description, and the literal
display/scan flags), nesting between subnets that are both in scope, and
every address inside them (hostname, description, MAC, owner, note, port,
gateway flag, ping/PTR flags) — plus **custom fields** on both. IPv4 and
IPv6 alike.

**Also replicated: folders, L2 domains, VLANs and VRFs**, and each
subnet's link to them. A phpIPAM folder is a `subnets` row with
`isFolder=1` and no network of its own, named by its `description`;
folders replicate by **path** (a list of names, since a folder name may
itself contain a slash), empty ones included, and a subnet inside one
lands inside it on the target. These are carried as records in their own right, so a VLAN or
VRF defined upstream but attached to nothing still appears on the target
— defining one is a deliberate act at the source, and a one-way link
gives the target no way to ask for it later. Their natural keys are a
domain by **name**, a VLAN by **(domain, number)**, and a VRF by
**(section, name)**; phpIPAM has no unique index on any of them, so the
same VLAN number in two domains, or the same VRF name in two sections,
are genuinely separate records. That is what keeps fan-in safe.

Scoping follows phpIPAM's own rule rather than anything this tool
invents: a VLAN belongs to a section through its L2 domain
(`vlanDomains.permissions`, which holds *section ids* despite the name),
and a VRF through its own `sections` list. Both lists are only ever
added to on the target, never replaced, so one subordinate's import
cannot evict another's.

**Not replicated:** devices, locations, nameservers, permissions,
customers — separate master data with their own ids.

**Deletions are not replicated by default** — see
[Strict mirror and drift](#strict-mirror-and-drift).

### Custom fields need "Nest custom fields" enabled

phpIPAM decides what is custom by diffing the live table against its
shipped schema, so a custom field's name is whatever you typed — a field
created as "Owner" is a column named `Owner`, with nothing marking it as
custom. Only with **Nest custom fields = Yes** on the API app does
phpIPAM report them distinctly — under a `custom_fields` key — and only
then can they be replicated. (Note that is a *read* format only: writes
must send them flat, which is how the snapshot stores them.)

Enable it on **both** sides. Off on the source and custom fields are
silently not carried; mismatched between the two and every custom field
is rewritten on every run.

**Custom fields are schema, not data.** This tool replicates their
values; it cannot create the fields. Any custom field used by a source
must already exist on the target with the same name, or every record
carrying it is rejected with `Invalid request key <name>`.

If a field you need is missing, check `dropped_source_fields` in the
manifest. Adding one is a one-line change to `SUBNET_SYNCED_FIELDS` or
`ADDRESS_SYNCED_FIELDS` — provided it is a literal value and not an id.

---

## Topologies

### 1:1 replication

One source, one replica. Configure both hosts with `config.example.yml`.

### Fan-in — many subordinates, one master

Several airgapped instances, each managing their own non-overlapping
networks, pushing up into a single read-only master. Subordinates are
configured exactly as above; the master uses
`config.master.example.yml`.

```
  site-a  ──(site-a-ipam.git, mirrored)──┐
  site-b  ──(site-b-ipam.git, mirrored)──┤
  site-c  ──(site-c-ipam.git, mirrored)──┼──▶  MASTER phpIPAM
  site-d  ──(site-d-ipam.git, mirrored)──┤     (read-only to users)
  site-e  ──(site-e-ipam.git, mirrored)──┘
```

The master syncs every subordinate in one command:

```bash
./ipam_import.py --config config.master.yml --pull            # dry run, all sites
./ipam_import.py --config config.master.yml --pull --apply    # for real
./ipam_import.py --config config.master.yml --source site-c   # just one
```

#### Each subordinate MUST own its own section on the master

This is the one rule that matters, and it is not a style preference.

Every import treats anything in its target section that is absent from
its snapshot as an orphan — reported as drift, and **deleted** once
`delete_drift` is on. If two subordinates shared a section, site A's sync
would see site B's subnets as records the snapshot no longer contains and
remove them. Every run, in both directions.

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

**The config loader refuses to start if two sources target the same
section** (case-insensitively, and counting unmapped names), so this
cannot be got wrong silently. It also refuses duplicate source names and
two sources reading the same directory.

#### Other fan-in behaviour

- **One broken subordinate does not stop the others.** A stale mirror,
  corrupt snapshot or missing section is reported and skipped; the rest
  still sync. Exit 1 if any source failed, 2 if individual records did.
- **Output is labelled per source** (`[site-a] ...`), so one log is
  readable.
- **Staleness is reported per source** — see
  [Monitoring](#monitoring-and-what-the-repo-cannot-tell-you).

---

## Usage

Requires Python 3.9+, `requests` and `PyYAML` on both hosts — the tool
checks the version at startup and refuses to run on anything older, so
that an unsupported interpreter fails immediately instead of part-way
through a run. Under cron, name the interpreter by absolute path rather
than relying on the shebang; cron's PATH is not your shell's. Full
procedure in [DEPLOYMENT.md](DEPLOYMENT.md); the essentials:

```bash
pip install -r requirements.txt
cp config.example.yml config.yml     # then edit
```

Both instances need a phpIPAM **API app** (Administration → API) in
app-code / static-token security mode — which **requires HTTPS**; phpIPAM
returns `503 "SSL connection is required for API"` over plain HTTP. The
source app needs Read; the target app needs Read/Write.

Tokens are never stored in `config.yml`. Each side names either an
environment variable (`token_env`) or a command whose stdout is the token
(`token_command`, so ansible-vault / pass / gpg all work).

**On the source:**

```bash
./ipam_export.py --config config.yml --out-dir /srv/ipam-data --commit --push
```

`--out-dir` is a path inside a working copy of the mirrored repo. It
commits only if something actually changed, so a five-minute cron does
not fill the repo with empty commits.

**On the target:**

```bash
# dry run -- writes nothing, prints the plan
./ipam_import.py --config config.yml --snapshot-dir /srv/ipam-data --pull

# once trusted
./ipam_import.py --config config.yml --snapshot-dir /srv/ipam-data --pull --apply
```

`--pull` uses `--ff-only`: the receiving side must never diverge, so a
local commit there is a loud failure rather than a merge.

**Exit codes:** `0` success · `1` could not proceed (bad config, corrupt
snapshot, missing section, a source failed, delete limit tripped) · `2`
applied, but individual records failed.

Wrap both in `flock` — there is no internal locking.

---

## Operating it

### Scale and cost per run

**Writes are diff-only; reads are not.** Every run walks the whole
dataset on both sides — one API call per subnet for its addresses — then
writes only what differs.

Measured on the lab stack (1.8.1, **204 subnets / 40,005 addresses**, one
host, no network latency):

| Operation | Time |
|---|---|
| Export (full read + snapshot + checksums) | ~7 s |
| Import, steady state — nothing changed | ~6 s |
| Import, incremental — 6 records changed | ~6 s |
| **Initial load** — 40,209 records created | **~16 min** (~43 writes/s) |

- **Routine syncs are cheap** — ~13 s per cycle for 40k records
  regardless of how much changed, so a 5-minute cron is comfortable.
- **The first load is not.** One POST per record. Run it by hand.

Cost scales with the **number of subnets** (`2 + N` calls per side), not
the number of addresses. Many small subnets cost more than a few large
ones. Add your round-trip time × the call count for a remote instance.

### Recovering from a mirror outage

Because the importer is state-based, a mirror that has been down for
weeks does **not** replay. Measured with `lab/verify-catchup.sh` — 14
days down, 50 address changes a day, 2,000-record estate:

| | |
|---|---|
| Backlog when the mirror returned | 14 commits |
| Edits made upstream | 700 |
| Runs needed to converge | **1** |
| Time to converge | **17 s** |
| Changes applied | 700 — bounded by the *net* diff |
| Created *and* deleted mid-outage | never appeared, not even in the plan |
| Immediate re-run | no-op |

A host renamed five times during the outage is written once, with its
final name. Worst case — every record changed — is bounded by a full
reload (~16 min per 40,000 records).

**The one surprise:** with `delete_drift` on, an outage that coincided
with mass deletion upstream produces a catch-up run that would delete
most of the target, and the safety limit **refuses the whole run**,
leaving the target untouched. That is correct — a long outage and a
mis-scoped snapshot look identical from the target's side.

### Repository growth

Measured with `lab/measure-growth.py`, simulating a year of churn on a
40,000-record estate:

| Churn | After 365 days | After `git gc` |
|---|---|---|
| 30 address changes/day | 27.5 MB | **2.6 MB** |
| 300 address changes/day | 38.4 MB | **12.2 MB** |

371 MB of uncompressed content packed to 2.3 MB in the low-churn case —
canonically-serialised JSON deltas extremely well. Ten years of heavy
churn lands around 120 MB.

**It needs no routine cleanup.** The larger figures are loose objects
awaiting git's own housekeeping. The master can also clone shallow, since
it never needs history (592 KB vs 2.7 MB). Detail, plus what truncating
history would cost across a one-way mirror, in
[DEPLOYMENT.md](DEPLOYMENT.md).

What keeps it small — do not break these: the exporter committing only on
real change, the canonical serialisation, and one file per subnet.

### Making the target read-only

The target is authoritative-from-the-source, so it usually makes sense to
stop people editing mirrored subnets by hand. phpIPAM's per-group
permissions do this and do **not** interfere with the sync. Verified on
1.8.1:

- **Group permissions do not gate the API app.** With every replicated
  subnet and the section set read-only for all groups, the importer still
  created, updated and read normally. The API app's own
  `app_permissions` is the only thing governing it.
- **The importer never touches `permissions`.** It is excluded from the
  field policy as a map of instance-local group ids, so whatever you set
  survives re-syncs.
- **New subnets inherit their parent's permissions**, so the arrangement
  is self-maintaining.

**The gotcha:** a nested subnet inherits from its **parent subnet**, not
the section. Setting only the section leaves children of a still-writable
parent writable. Confirmed — section at `{"2":"1","3":"1"}`, parent
subnet at `{"2":"3"}`, and a newly replicated child came out `{"2":"3"}`.

So set the permission on **the section and every existing top-level
subnet** in it.

You will still need write access yourself to action the drift report.

### Strict mirror and drift

With deletion off, the target accumulates records the source no longer
has, plus anything created locally. Every run reports them:

```
drift_address  10.20.5.0/24 10.20.5.10  -- exists on target, absent from
                                           snapshot (not deleted --
                                           deletion is disabled)
```

That report is the intended mechanism for handling deletions by hand.
`--quiet-drift` suppresses it. Leaving deletion off is safer, but not
free: the target progressively **over-reports utilisation** as freed
addresses accumulate.

To make the target track deletions:

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

Three guards:

1. **`--apply` is still required**, and deletions print in their own
   unmissable `DELETIONS` block.
2. **Scope is unchanged** — only configured sections, only what is
   inside them.
3. **A safety limit**, with a floor of 5 records so small datasets are
   not permanently blocked.

The third is the one that matters. The realistic disaster is not one
record wrongly deleted — it is an empty or mis-scoped snapshot arriving
over a link the target cannot question. When it trips you get exit 1,
nothing is written, and a message pointing at the likely cause:

```
Refusing to run: this would delete 61 of 69 in-scope record(s) on the
target (88%), which is over the safety limit of 6 (10%, floor 5).
This usually means the snapshot is empty, truncated, or was exported
from the wrong section -- NOT that this much really was deleted
upstream. Check the source instance and the snapshot's manifest.json
first.
```

`--force-delete` overrides for one run — but check *why* first.

Deletion order is addresses first, then subnets deepest-first, so a
parent is never removed while a child still points at it.

Strict mirror also enforces read-only intent: anything created by hand on
the target is removed on the next sync.

### Monitoring, and what the repo cannot tell you

**The target cannot detect that a source has stopped exporting.** It
keeps applying the last good snapshot and reporting success.

This is inherent, not an oversight. The exporter deliberately does not
rewrite an unchanged snapshot, so `manifest.json`'s `exported_at` means
*content last changed*, not *the exporter ran*. Nothing crossing a
change-only channel can mean both.

Two mitigations, use both:

1. **Monitor the export job on each source** (cron exit status). This is
   the real signal.
2. **`--stale-after-days`** (default 30) warns when a snapshot's content
   has not changed in that long. A genuinely static site trips it too, so
   treat it as a prompt to check.

Also watch for: `applied N change(s)` unexpectedly large in steady state
(something is being rewritten every run), and the same record erroring
every run (a permanently failing record alerts every cycle until fixed).

---

## Trust model

Worth being explicit about, because the direction of trust is the whole
architecture: **the master trusts every subordinate that can write to a
mirrored repo.** A snapshot is a set of instructions to write to the
master, and the importer's job is to carry them out.

What that means in practice:

- **The mirrored repo is the security boundary.** Anyone who can commit
  to a subordinate's repo can add or update records in that
  subordinate's section on the master — and, with
  `options.delete_drift` on, delete them. Protect write access to those
  repos the way you would protect write access to the master's database.
  Deploy keys should be read-only on the master side; it never needs to
  push.
- **Checksums are integrity, not authenticity.** `manifest.json` lists
  both the paths and their digests, so a self-consistent manifest is
  easy to author. The digests catch a truncated or corrupted transfer.
  They do not tell you the snapshot came from your exporter. If you need
  that, sign the commits on the subordinate and verify the signature
  before importing (`git verify-commit HEAD`) — the importer does not do
  this for you.
- **The importer will not read outside its snapshot directory.** A
  manifest naming `../../etc/passwd`, an absolute path, or a symlink out
  of the tree is refused even when its digest matches, so a hostile
  manifest cannot turn repo write access into arbitrary file reads on
  the master. Enforced by `_safe_relative_path` and tested in
  `tests/test_snapshot.py`.
- **Each source is confined to its own section.** The config loader
  refuses two sources that target the same section, because each import
  treats anything in its section that is absent from its snapshot as
  drift — so a shared section would let one site delete another's
  networks. See `_validate_sources` in `ipamsync/config.py`.
- **`token_command` runs a command.** It exists so any secret store can
  be used, but it means whoever can edit `config.yml` can execute code
  as the sync user. The config is meant to be committed, so treat write
  access to it accordingly.
- **Blast radius is bounded by the API app's rights.** The exporter's
  app should be read-only (`Read`); only the importer's needs
  `Read/Write`. Additive-only mode (the default) keeps the worst case at
  "wrong records added or updated" rather than "records destroyed"; see
  [Strict mirror and drift](#strict-mirror-and-drift).
- **"Read-only master" means read-only to users, not to admins.**
  phpIPAM grants an Administrator read/write/admin on every section
  before it consults any permission, so no setting inside phpIPAM makes
  mirrored data read-only to one. Enforcing that needs database grants
  and a second phpIPAM configuration — see DEPLOYMENT.md § 2.5b, which
  weighs whether it is worth it.

Tokens are never read from the config file itself, are passed as headers
rather than on the command line, and are kept out of error messages —
a failing `token_command` reports its stderr, never its stdout.

---

## What real phpIPAM taught us

Six bugs that no amount of testing against a fake would have caught,
because the fake behaved the way phpIPAM's *documentation* implies. All
six are documented with the exact requests and responses in
[`lab/README.md`](lab/README.md).

1. **Empty collections are HTTP 404**, not an empty list. Broke exporting
   any supernet, and the *first* import into an empty section.
2. **Subnets are read as `tag` but written as `state`.** Every subnet
   create and update failed until mapped.
3. **Custom fields have no distinguishing name**, so prefix-based
   detection silently missed real ones.
4. **An endless update loop on custom fields** — the target also returns
   them nested, so every run rewrote every custom field forever. Nothing
   was wrong in the data, which is exactly why it would have survived a
   casual look.
5. **Overlap is validated on create but not on update.** A new subnet
   that overlaps a sibling is refused with `409`, while `PATCH
   masterSubnetId` accepts anything — so inserting an intermediate
   aggregate upstream could not be applied naively, and re-parenting has
   to detach before it creates.
6. **A folder is a subnet row that the subnet endpoint will not serve.**
   `GET subnets/{id}/` on a folder answers `404`, and phpIPAM lists
   folders *first* — which quietly broke custom-field discovery, since it
   probes the first record it sees.

The lesson generalises: the write path of a client built from
documentation is unproven until it has run against the real server.

---

## Testing

```bash
python3 -m unittest discover -s tests -v          # 172 tests, no network
```

The suite includes **deliberate negative controls** throughout — every
"these compare equal" assertion is paired with one proving a real
difference is still detected. A field-diffing bug that silently stopped
replicating changes would otherwise pass a happy-path suite forever.

- `tests/test_end_to_end.py` runs the real exporter and importer against
  an in-memory fake that reproduces phpIPAM's actual quirks, including
  its inconsistent custom-field serialisation. Headline assertion:
  `test_second_import_is_a_no_op`.
- `tests/test_http_roundtrip.py` runs the full stack over a real socket.
- `tests/test_examples.py` verifies the committed example snapshot, so
  the documentation cannot drift from the code.
- `lab/` runs everything against real phpIPAM.

Claims here are mutation-tested rather than assumed. Each of these
independently fails the suite: neutering the field diff, removing
checksum verification, carrying a source id across, renaming the auth
header, reverting the `tag`→`state` alias, disabling custom-field
discovery, removing the delete safety limit, and removing the fan-in
section-collision guard.

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
| `DEPLOYMENT.md` | Step-by-step setup, cron, monitoring, runbook |
| `examples/` | A real snapshot, annotated — verified by the tests |
| `lab/` | Three phpIPAM instances in Docker + `verify.sh` (1:1), `verify-fanin.sh`, `verify-catchup.sh`, `measure-growth.py` |

`phpipam_client.py` is a standalone phpIPAM API client and is kept free
of anything sync-specific, so it stays independently reusable. The
empty-collection handling and section creation this tool needs are added
by subclass in `client_ext.py` rather than by editing it.

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).

This project is not affiliated with the phpIPAM project. It talks to
phpIPAM's public REST API and ships no phpIPAM code.
