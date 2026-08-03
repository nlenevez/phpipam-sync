# Deployment guide

Step-by-step setup for the fan-in topology: several airgapped phpIPAM
installations, each owning its own networks, replicating up into one
read-only master over one-way git mirroring.

```
  SUBORDINATE (×N, airgapped)              MASTER

  phpIPAM ──export──▶ site-a-ipam.git ─mirror─▶ site-a-ipam.git ──import──▶ phpIPAM
  (owns its networks)                                                       (read-only
                                                                             to users)
```

Everything below was verified against phpIPAM **1.8.1** (also 1.7.4). If
you run something else, read [Before you start](#before-you-start) —
phpIPAM's API differs between versions in ways that break this tool, and
the checks there are how you find out cheaply.

**Contents**

1. [Before you start](#before-you-start)
2. [Part 1 — each subordinate](#part-1--each-subordinate)
3. [Part 2 — the master](#part-2--the-master)
4. [Part 3 — first load](#part-3--first-load)
5. [Part 4 — automate](#part-4--automate)
6. [Part 5 — switching to strict mirror](#part-5--switching-to-strict-mirror-later)
7. [Monitoring](#monitoring)
8. [When the mirror fails and then catches up](#when-the-mirror-fails-and-then-catches-up)
9. [Repository growth and maintenance](#repository-growth-and-maintenance)
10. [Runbook](#runbook)
11. [Checklists](#checklists)

---

## Before you start

**Requirements on every host running the tool**

- Python 3.9+, `requests`, `PyYAML` (`pip install -r requirements.txt`).
  Check the interpreter cron will actually use, not just your shell's —
  see Part 4. The tool refuses to start on anything older, by design:
  under 3.6 an export otherwise completed its whole read and then died in
  the git plumbing with an unrelated-looking `TypeError`.
- `git`, with the mirrored repo already cloned locally
- **HTTPS to the phpIPAM instance.** Not optional: phpIPAM refuses
  app-code API authentication over plain HTTP with
  `503 "SSL connection is required for API"`. Behind a reverse proxy,
  set `IPAM_TRUST_X_FORWARDED=true` (or `$trust_x_forwarded_headers`)
  so phpIPAM honours `X-Forwarded-Proto`.

**Enable the API** on every instance: Administration → phpIPAM settings →
API = On. Without it every call returns `503 "API server disabled"`.

**Prove your version behaves.** Two of the four bugs found building this
were version-specific API quirks. Before deploying, run the lab against
your version:

```bash
cd lab
PHPIPAM_VERSION=v1.8.1 ./setup.sh     # use YOUR version tag
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
export PHPIPAM_DST_TOKEN=DSTTOKEN0000000000000000000000
./verify.sh && ./verify-fanin.sh
docker compose down
```

If those pass, the tool works against that version. If they fail, the
output names what differs — do not deploy until it is understood.

---

## Part 1 — each subordinate

Repeat for every site. Nothing here differs between them except names.

### 1.1 Create the API app (read-only)

Administration → API → Create API app:

| Field | Value |
|---|---|
| App id | `sync` |
| App security | **SSL with App code** |
| App permissions | **Read** |
| Nest custom fields | **Yes** ← see below |

Copy the generated **App code**; that is the token.

**Read** is deliberate. The exporter never writes, and a read-only token
makes phpIPAM enforce that rather than you trusting it. If the exporter
ever attempted a write it would fail with 401.

**Nest custom fields = Yes** is required if you use custom fields at all.
phpIPAM decides what is custom by diffing the live table against its
shipped schema, so a field you created as "Owner" is a column named
`Owner` with nothing marking it as custom. Only with nesting on does
phpIPAM report them distinctly, and only then can they be replicated.
Without it they are indistinguishable from phpIPAM fields the tool has
not been taught about, and are reported in `dropped_source_fields`
instead of carried.

### 1.2 Decide what to replicate

The tool replicates **whole sections**. Either point it at the section
your networks already live in, or make a dedicated one.

Note the section name — it goes in `sections:` below, and the master maps
it to that site's own section.

### 1.3 Install

```bash
sudo install -d -o ipamsync -g ipamsync /opt/phpipam-sync /srv/ipam-data
git clone <this repo> /opt/phpipam-sync
cd /opt/phpipam-sync && pip install -r requirements.txt

# the repo that mirrors up to the master
git clone git@<this-site-gitlab>:ipam/site-a-ipam.git /srv/ipam-data
```

### 1.4 Configure

`/opt/phpipam-sync/config.yml` — from `config.example.yml`:

```yaml
source:
  base_url: https://ipam.site-a.internal    # no trailing slash, no /api
  app_id: sync
  token_env: PHPIPAM_SRC_TOKEN
  verify_ssl: true                          # keep true in production
  timeout: 30

sections:
  - Networks                                # as named on THIS instance

options:
  sync_tags: true          # false if this instance has custom ipTags
```

The token is never stored in the config. Supply it with `token_env`, or
`token_command` if you keep it in a secret store:

```yaml
  token_command: "ansible-vault view /etc/phpipam-sync/token"
```

### 1.5 First export

```bash
export PHPIPAM_SRC_TOKEN='<app code>'
cd /opt/phpipam-sync
./ipam_export.py --config config.yml --out-dir /srv/ipam-data
```

[`examples/`](examples/) shows a complete, real snapshot with a
commentary on each file — worth a look before your first export, so you
know what you are checking against.

Read the output before going further:

- **`custom subnet field(s): ...`** — nesting is on and custom fields
  were found. Note these names; the master needs the same fields.
- **`note: ... field(s) present on the source but not replicated`** —
  something is not being carried. If it mentions nesting, turn on "Nest
  custom fields" and re-run. Otherwise the named fields are phpIPAM
  fields this tool does not know; check whether you need them.
- **Subnet/address counts** — sanity-check against the UI.

Then commit and push:

```bash
./ipam_export.py --config config.yml --out-dir /srv/ipam-data --commit --push
```

### 1.6 Confirm the mirror

On the master, confirm the repo arrived and the commit matches. Until
that works there is no point configuring the master side.

---

## Part 2 — the master

### 2.1 Create one section per subordinate

Administration → Sections → Create:

| Section | Description |
|---|---|
| `Site-A` | Mirrored from site A |
| `Site-B` | Mirrored from site B |
| … | one per subordinate |

**This is the single most important step.** Each import treats anything
in its target section that is absent from its snapshot as an orphan —
reported as drift, and deleted once strict mirror is on. If two
subordinates shared a section, site A's sync would see site B's subnets
as orphans and remove them.

The tool refuses to start if two sources target the same section, so you
cannot get this wrong silently — but understand why before overriding
anything.

**Turn OFF "strict mode" on each replicated section.** It is on by
default. Strict mode makes phpIPAM validate every *created* subnet
against its siblings and refuse overlaps, which blocks one ordinary
upstream change: adding an aggregate above subnets that already exist at
the same level. The importer reorders what it can (see "Re-parenting" in
`ipamsync/plan.py`), but when the new aggregate would sit above
*top-level* subnets there is nowhere to move them to first, and the
create fails with `409 ... overlaps with ...`.

Turning it off is safe for a section that only this tool writes to: the
hierarchy being written is one the source instance already validated
under its own strict mode. Confirmed on 1.8.1 — the same create returns
`409` with strict mode on and `201` with it off. It is per-section, so
sections you manage by hand keep their checks.

It also matters once VRFs are in play: phpIPAM's overlap check is
VRF-aware, so subnets that legitimately overlap in different VRFs
upstream will be refused on the master during any window where the VRF
assignment has not yet been replicated.

Set each section's permissions to **read** for your user groups now (see
2.5).

### 2.2 Create the custom fields

**Custom fields are schema, not data.** This tool replicates their
*values*; it cannot create the fields. Any custom field used by any
subordinate must already exist on the master with the **same name**, or
every record carrying it is rejected with:

```
create_subnet 10.20.0.0/16: POST subnets/ failed: Invalid request key Owner
  ('Owner' exists on the source instance but not on this one. If it is a
  custom field, create it here too under Administration > Custom fields...)
```

Administration → Custom fields → add the same fields, same names, for
both `subnets` and `addresses` as needed. The names came from step 1.5.

### 2.3 Create the API app (read-write)

Administration → API → Create API app:

| Field | Value |
|---|---|
| App id | `sync` |
| App security | **SSL with App code** |
| App permissions | **Read / Write** |
| Nest custom fields | **Yes** |

Nesting matters on this side too: with it off, the master returns custom
fields in a different shape from the snapshot and the importer rewrites
every custom field on every run forever.

### 2.4 Clone every mirrored repo

```bash
sudo install -d -o ipamsync -g ipamsync /srv/ipam
for site in a b c d e; do
  git clone git@<master-gitlab>:ipam/site-$site-ipam.git /srv/ipam/site-$site-ipam
done
```

One directory per subordinate. The tool refuses two sources pointing at
the same directory.

### 2.5 Make the master read-only to users

Users should never enter data here. phpIPAM's group permissions do this
and do **not** interfere with the sync — group permissions gate the UI,
while the API app's own permission governs the importer. Verified: with
every subnet set read-only for all groups, the importer still created and
updated normally.

Two things to know:

- **New subnets inherit their parent's permissions**, so this is
  self-maintaining — set it once and subnets the sync creates later come
  out read-only too.
- **A nested subnet inherits from its parent *subnet*, not the section.**
  Setting only the section leaves children of a still-writable parent
  writable. Set the permission on **the section and every existing
  top-level subnet in it.**

The importer never touches `permissions`, so whatever you set survives.

You will still need write access yourself to action the drift report.

### 2.5b Read-only to administrators as well (optional)

Step 2.5 stops at non-admin users, and that is not a configuration
oversight — it is the ceiling of what phpIPAM can express. Both
permission checks open with a hard-coded bypass:

```php
// functions/classes/class.Sections.php:456, class.Subnets.php:3025
# if user is admin then return 3, otherwise check
if($user->role == "Administrator")	{ return 3; }
```

Level 3 is read/write/admin, returned before any lookup of the section's
or subnet's own permissions. phpIPAM has no lock flag, no immutable
state, and no per-subnet override that an administrator does not
outrank. **If you need replicated data to be read-only to admins too,
that has to be enforced underneath phpIPAM, in the database.**

That forces one structural decision. The importer writes through the
same phpIPAM code an administrator uses, so the database has to be able
to tell them apart — which means two MySQL users, which means two
phpIPAM configurations sharing one database:

| | reached by | MySQL user | rights |
|---|---|---|---|
| **Private instance** | the importer only — bind to localhost or a management VLAN | `ipam_sync` | full DML |
| **Public instance** | people | `ipam_ro` | restricted, below |

Both are the same phpIPAM release pointed at the same schema; only
`config.php` differs. The importer's API app lives on the private one.

#### Option A — table grants (whole master read-only)

If the master holds nothing but replicated data — the normal case for
this topology — this is the smallest thing that actually works:

```sql
CREATE USER 'ipam_ro'@'%' IDENTIFIED BY '...';
GRANT SELECT ON phpipam.* TO 'ipam_ro'@'%';
-- housekeeping phpIPAM still needs in order to function
GRANT INSERT, UPDATE, DELETE ON phpipam.php_sessions  TO 'ipam_ro'@'%';  -- if DB sessions are on
GRANT INSERT                 ON phpipam.logs          TO 'ipam_ro'@'%';
GRANT INSERT, UPDATE, DELETE ON phpipam.loginAttempts TO 'ipam_ro'@'%';
GRANT UPDATE (lastLogin)     ON phpipam.users         TO 'ipam_ro'@'%';
```

`subnets`, `ipaddresses`, `sections`, `vlans` and `vrf` get no write
grant, so an administrator editing a replicated subnet gets a database
error instead of a saved change.

Include the column-level `lastLogin` grant. Without it every login
paints a red banner: `update_login_time()` catches the failure rather
than dying, but it does surface it.

#### Option B — triggers (per-section granularity)

Only needed if the master must also carry locally-editable sections,
since grants cannot distinguish one section's rows from another's:

```sql
DELIMITER //
CREATE TRIGGER subnets_ro BEFORE UPDATE ON subnets FOR EACH ROW
BEGIN
  DECLARE msg VARCHAR(255);
  IF OLD.sectionId IN (SELECT id FROM sections WHERE name LIKE 'Site-%')
     AND SESSION_USER() NOT LIKE 'ipam\_sync@%' THEN
    SET msg = 'replicated subnet: read-only, edit it at the source';
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = msg;
  END IF;
END//
DELIMITER ;
```

Two details that will cost you an afternoon otherwise, both confirmed
against MySQL 8:

- **The bypass must test `SESSION_USER()`, not `CURRENT_USER()`.**
  Triggers execute as their definer, so inside one `CURRENT_USER()`
  reports `root@localhost` no matter who connected, and the exemption
  silently never matches. `SESSION_USER()` reports the account that
  actually opened the connection.
- **`SIGNAL ... SET MESSAGE_TEXT` will not take an expression.** Build
  the message into a declared variable first; `CONCAT(...)` inline is a
  syntax error.

You need this on `subnets` and `ipaddresses`, for INSERT and DELETE as
well as UPDATE.

#### What this buys, and what it does not

It makes changing replicated data **deliberate rather than possible**.
Anyone holding MySQL credentials can drop a trigger or re-grant, and
nothing here changes that. What it removes is the accidental case: no
one edits mirrored data by clicking around in the UI and wondering later
why the next import reverted it.

Costs worth weighing before you take this on:

- Failures surface as raw database errors, not a civil "this is
  read-only" message.
- phpIPAM upgrades run DDL. Run them against the private instance, as
  the privileged user.
- Managing custom fields does `ALTER TABLE`, so administrators lose that
  on the public instance. Arguably correct — fields have to match the
  source anyway (step 2.2) — but it is a real change to how you work.

**Recommendation.** If the master is purely replicated, take Option A
and simply do not hand out Administrator on the public instance
day-to-day; keep one break-glass account. That needs no triggers to
maintain, and the grants mean even the break-glass account cannot
quietly edit subnet data. Reach for Option B only when the master has to
hold local sections alongside mirrored ones.

**Verification status.** The phpIPAM behaviour above is quoted from
1.8.1's source, and the two MySQL gotchas were reproduced on MySQL 8.
The arrangement as a whole has **not** been exercised in the lab — the
lab runs a single instance per database, so the two-config split is
described here as a design, not as something demonstrated end to end.
Treat the grant list as the starting point for your own test rather than
a finished recipe, and prove it on a copy before you rely on it.

### 2.6 Configure

`/opt/phpipam-sync/config.master.yml` — from `config.master.example.yml`:

```yaml
target:
  base_url: https://ipam-master.internal
  app_id: sync
  token_env: PHPIPAM_MASTER_TOKEN
  verify_ssl: true
  timeout: 30

sources:
  - name: site-a
    snapshot_dir: /srv/ipam/site-a-ipam
    sections: [Networks]              # as exported by site A
    section_map: {Networks: Site-A}   # where it lands here

  - name: site-b
    snapshot_dir: /srv/ipam/site-b-ipam
    sections: [Networks]
    section_map: {Networks: Site-B}
  # ... one block per subordinate

options:
  sync_tags: true
  create_missing_sections: false   # keep false: you made them by hand
  delete_drift: false              # start additive; see Part 5
  delete_limit_fraction: 0.1
```

### 2.7 Dry run

```bash
export PHPIPAM_MASTER_TOKEN='<app code>'
cd /opt/phpipam-sync
./ipam_import.py --config config.master.yml --pull
```

**Writes nothing.** Read it site by site:

- Each `[site-x]` block should only mention that site's networks. If
  site A's block mentions site B's addresses, the sectioning is wrong —
  fix it before applying anything.
- `create_subnet` / `create_address` counts should match what that
  subordinate exported.
- Any `ERROR` naming `Invalid request key <field>` means a missing
  custom field (step 2.2).

---

## Part 3 — first load

Do this **by hand**, not from cron. The initial load is one API POST per
record: roughly **43 writes/second**, so ~16 minutes per 40,000 records,
longer over a network. Routine syncs afterwards take seconds.

```bash
./ipam_import.py --config config.master.yml --pull --apply --source site-a
```

One site at a time for the first load, so a problem is easy to attribute.
Check the master's UI, then move to the next site.

Once all sites are loaded, confirm a re-run is a no-op:

```bash
./ipam_import.py --config config.master.yml --pull --apply
# ... every site should say: changes: none -- target already matches the snapshot
```

If it is *not* a no-op, stop and investigate — something is being
rewritten on every run, which would churn the master's history forever.

---

## Part 4 — automate

### On each subordinate

```cron
*/5 * * * * ipamsync /usr/bin/flock -n /var/lock/ipam-export.lock \
  /usr/bin/python3.12 /opt/phpipam-sync/ipam_export.py \
  --config /opt/phpipam-sync/config.yml \
  --out-dir /srv/ipam-data --commit --push >> /var/log/ipam-export.log 2>&1
```

Export is read-only against phpIPAM and commits only when something
actually changed, so a 5-minute interval does not fill the repo with
empty commits.

**Name the interpreter by absolute path**, as above, rather than relying
on the `#!/usr/bin/env python3` shebang. cron's PATH is not your login
shell's, and a host that has 3.12 installed can still resolve `python3`
to something older — on which this tool refuses to start. Substitute the
real path from `command -v python3.12` on that host.

### On the master

```cron
*/10 * * * * ipamsync /usr/bin/flock -n /var/lock/ipam-import.lock \
  /usr/bin/python3.12 /opt/phpipam-sync/ipam_import.py \
  --config /opt/phpipam-sync/config.master.yml \
  --pull --apply --quiet-drift >> /var/log/ipam-import.log 2>&1
```

**`flock` is not optional.** The tool has no internal locking, and a
master pulling several sites can take a while. `-n` skips the run if the
previous one is still going.

The token has to reach cron. Either an `EnvironmentFile` with a systemd
timer, or `token_command` in the config pointing at your secret store —
do not put it in the crontab.

### systemd timer alternative

```ini
# /etc/systemd/system/ipam-import.service
[Service]
Type=oneshot
User=ipamsync
EnvironmentFile=/etc/phpipam-sync/master.env      # PHPIPAM_MASTER_TOKEN=...
ExecStart=/usr/bin/flock -n /var/lock/ipam-import.lock \
  /opt/phpipam-sync/ipam_import.py --config /opt/phpipam-sync/config.master.yml \
  --pull --apply --quiet-drift
```

```ini
# /etc/systemd/system/ipam-import.timer
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
```

---

## Part 5 — switching to strict mirror (later)

While `delete_drift` is false the master **accumulates** records the
subordinates have deleted, so it progressively over-reports utilisation.
The drift report shows what has built up.

Before enabling deletion, run additive for a few weeks and read the drift
reports (drop `--quiet-drift`). On a correctly-sectioned master, site A's
drift should only ever mention site A's networks. If another site's
records appear there, the sectioning is wrong — find that out while
deletion is still off.

When ready:

```bash
# what would be deleted -- writes nothing
./ipam_import.py --config config.master.yml --pull --delete

# then, once you agree with the list
./ipam_import.py --config config.master.yml --pull --delete --apply
```

Expect the first strict run to delete everything that accumulated while
additive. Then set `delete_drift: true` in the config.

Strict mirror also **enforces the read-only intent**: anything created by
hand on the master is removed on the next sync.

A run that would delete more than `delete_limit_fraction` (default 10%)
of the in-scope records is refused outright, writing nothing:

```
Refusing to run: this would delete 61 of 69 in-scope record(s) on the target
(88%), which is over the safety limit of 6 (10%, floor 5).
This usually means the snapshot is empty, truncated, or was exported from the
wrong section -- NOT that this much really was deleted upstream.
```

`--force-delete` overrides it — but find out *why* first. The usual cause
is a bad snapshot, not a real bulk deletion.

---

## Monitoring

### Exit codes

| Code | Meaning | Action |
|---|---|---|
| 0 | Success | — |
| 1 | Could not proceed: bad config, corrupt snapshot, missing section, a source failed, delete limit tripped | Investigate; nothing was written for that source |
| 2 | Applied, but individual records failed | Read the named records; usually a missing custom field |

Alert on non-zero from both the export and import jobs.

### What the repo cannot tell you

**The master cannot detect that a subordinate has stopped exporting.** It
keeps applying the last good snapshot and reporting success. This is
inherent: the exporter deliberately does not rewrite an unchanged
snapshot, so `exported_at` means "content last changed", not "the
exporter ran".

Two mitigations, use both:

1. **Monitor the export job on each subordinate** (cron exit status).
   This is the real signal.
2. **`--stale-after-days`** (default 30) warns when a site's snapshot
   content has not changed in that long. A genuinely static site will
   trip it too, so treat it as a prompt to check, not an alarm.

### Watch for

- `applied N change(s)` where N is unexpectedly large in steady state —
  something is being rewritten every run.
- The same record erroring every run — a permanently failing record
  produces an alert every cycle until fixed.
- Drift growing without bound while additive.

---

## When the mirror fails and then catches up

An airgapped link will break. The worry is that after a fortnight down,
the target gets hit by a heap of changes across a heap of files.

It does not, and the reason is structural: **the importer is state-based,
not event-based.** It reads the snapshot tree at `HEAD` and never looks
at commit history. Two weeks of commits therefore collapse into a single
reconciliation against the final state, sized by the **net difference**,
not by how many commits or intermediate edits happened.

`lab/verify-catchup.sh` demonstrates this end to end — 14 days of outage
with 50 address changes a day against a 2,000-record estate:

| | |
|---|---|
| Backlog when the mirror returned | 14 commits |
| Edits made upstream during the outage | 700 |
| Runs needed to converge | **1** |
| Time to converge | **17 s** |
| Changes applied | 700 (bounded by the net diff) |
| Records created *and* deleted mid-outage | never appeared, not even in the plan |
| Immediate re-run afterwards | no-op |

### What this means in practice

- **Nothing is replayed.** The target does not walk the intermediate
  states. A host renamed five times during the outage is written once,
  with its final name.
- **Churn that cancelled out costs nothing.** An address created on day 3
  and deleted on day 9 never reaches the target at all — it is not in the
  final tree, so it is not in the plan.
- **The catch-up run is bounded by a full reload.** Worst case — every
  record changed — is the same as the initial load: roughly 43 writes a
  second, so ~16 minutes per 40,000 records. A realistic fortnight
  touching a few per cent of the estate is a minute or two.
- **`flock` matters here.** A long catch-up run must not overlap the next
  cron tick. That is what `flock -n` is for (Part 4). Without it, two
  runs would race.
- **Run it by hand the first time after a long outage.** Not because it
  is unsafe, but because it is the moment you most want to read the plan
  before applying it. `--pull` without `--apply` costs nothing.

### The one thing that will surprise you

If `delete_drift` is on and the outage coincided with a lot of records
being **deleted** upstream, the catch-up run will want to delete them all
at once — and the safety limit will **refuse the whole run** rather than
apply it:

```
Refusing to run: this would delete 1847 of 2004 in-scope record(s) on the
target (92%), which is over the safety limit of 200 (10%, floor 5).
```

This is deliberate, and verified: the target is left **completely
untouched**. A long outage and a mis-scoped snapshot look identical from
the target's side, so it stops and asks rather than guessing. Confirm the
deletions are genuine — check the source, check `manifest.json` — and
then re-run with `--force-delete`.

### Audit trail

The master's phpIPAM edit history will show one batch of changes at
catch-up, not the day-by-day sequence. **The git repo is the audit
trail** — it has every intermediate commit, one file per subnet, with the
exact before/after of each change. That is a better record than phpIPAM's
own log, but it lives in the repo, so do not truncate history you might
need (see below).

## Repository growth and maintenance

The obvious worry is that a year of moves, adds and changes turns the
mirrored repo into something enormous. Measured, it does not.

`lab/measure-growth.py` simulates a year of churn against a 40,000-record
estate, driving the real `write_snapshot()` so the layout and
serialisation are exactly what production produces:

| Churn | After 365 days | After `git gc` |
|---|---|---|
| 30 address changes/day | 27.5 MB | **2.6 MB** |
| 300 address changes/day | 38.4 MB | **12.2 MB** |

In the low-churn case that is 371 MB of uncompressed content across
10,780 blobs, packed down to 2.3 MB. Canonically-serialised JSON with
sorted keys deltas extremely well — small changes to a mostly-identical
file are close to free. **Ten years of heavy churn would still be around
120 MB.**

Re-run it against your own estate and churn rate:

```bash
./lab/measure-growth.py --subnets 400 --per-subnet 120 \
                        --changes-per-day 500 --days 365
```

### What actually needs doing

**Mostly nothing.** Git's automatic housekeeping packs loose objects as
they accumulate, and GitLab runs its own housekeeping server-side. The
larger "after 365 days" figures above are loose objects waiting to be
packed, not permanent growth.

If you want to be explicit about it, a monthly `git gc` on each working
clone costs nothing:

```cron
17 3 1 * * ipamsync git -C /srv/ipam-data gc --quiet --prune=now
```

**The master never needs history.** It only ever reads the current tree,
so it can clone shallow — 592 KB against 2.7 MB on the simulated repo:

```bash
git clone --depth 1 <url> /srv/ipam/site-a-ipam
```

`git pull --ff-only` (what `--pull` runs) works fine on a shallow clone
and keeps the shallow boundary — verified. It does accumulate the commits
it pulls, so if you want it to stay minimal, re-shallow occasionally:

```bash
git -C /srv/ipam/site-a-ipam fetch --depth=1 origin main
git -C /srv/ipam/site-a-ipam reset --hard origin/main
git -C /srv/ipam/site-a-ipam reflog expire --expire=now --all
git -C /srv/ipam/site-a-ipam gc --prune=now
```

### What keeps it small (don't break these)

- **The exporter commits only on a real change.** Unchanged data
  re-exports byte-identically, so an idle site costs nothing however
  often cron runs. This is load-bearing: an earlier bug where
  `manifest.json` carried a timestamp meant *every* run committed, which
  at a 5-minute cron would have been ~288 empty commits a day. There is a
  regression test for it.
- **Canonical serialisation** (sorted keys, fixed indent, numeric IP
  order) is what makes the deltas small. Serialising differently would
  cost far more than it looks.
- **One file per subnet** means a change touches one small file, not one
  huge one.

### If you genuinely need to truncate history

You probably will not, but if compliance or a one-off event (a site-wide
renumber) demands it, understand the cost first: **rewriting history
breaks the mirror.** The mirror will need a force push, and every
consumer must re-clone. Across an air gap that is a coordinated
operation, not routine maintenance.

The least disruptive form is to start a fresh root commit rather than
rewrite:

```bash
# On the SUBORDINATE, after confirming the master is fully caught up.
cd /srv/ipam-data
git checkout --orphan truncated
git add -A
git commit -m "snapshot history restarted $(date -I)"
git branch -M truncated main
git push --force origin main
```

Then on the master, delete and re-clone that site's directory. Run the
importer **without `--apply`** first: the plan should be empty, because
the snapshot content has not changed — only its history. If the plan is
not empty, stop and find out why before applying.

Do not do this while `delete_drift` is on unless you have re-cloned the
master's copy first — a half-transferred fresh history could look like a
mass deletion. (The delete safety limit would refuse it, which is exactly
what it is for, but do not rely on that as the plan.)

## Runbook

**`503 "API server disabled"`** — API not enabled in Administration →
phpIPAM settings.

**`503 "SSL connection is required for API"`** — app-code auth needs
HTTPS. Behind a proxy, set `IPAM_TRUST_X_FORWARDED=true` and forward
`X-Forwarded-Proto`.

**`415 "Invalid Content type <value>"`** — phpIPAM refused the request
content type. It accepts `application/json`, `application/xml`,
`application/x-www-form-urlencoded`, an absent header, and an empty one;
everything else is refused. This tool sends `application/json` on every
request, so seeing this means something between it and phpIPAM is
supplying or rewriting the header — nginx, `fastcgi_param CONTENT_TYPE`,
a reverse proxy, or a WAF.

The message ends with the offending value, so read it first. **If it
looks like it has nothing after it, that is itself the clue**: a
whitespace-only value prints that way, and so does an empty one on
phpIPAM ≤ 1.7.x (see below). Find what is arriving by logging it in
nginx —

```nginx
log_format ct '$remote_addr "$request" $status ct="$http_content_type"';
access_log /var/log/nginx/ct.log ct;
```

— and check for anything setting it:
`grep -rniE 'content.type|fastcgi_param CONTENT_TYPE' /etc/nginx/`.

Confirm the fix end to end from the phpIPAM host itself, which also
takes any outbound proxy out of the picture:

```bash
curl -sk -H "token: $TOKEN" https://127.0.0.1/api/<app_id>/sections/ | head -c 200
curl -sk -H "token: $TOKEN" -H 'Content-Type: application/json' \
     https://127.0.0.1/api/<app_id>/sections/ | head -c 200
```

**On phpIPAM ≤ 1.7.x specifically**, an *empty* value is refused too, on
PHP 8 only: the check reads `strlen(@$ct==0)` — the `==0` inside
`strlen()` — fixed to `strlen(@$ct)==0` in 1.8.1. On PHP 7 that evaluates
`strlen(true)` → 1, so the branch always fired and the check passed
everything; PHP 8 made `"" == 0` false, so it starts enforcing. Those
versions break on a **PHP** upgrade, not a phpIPAM one. Upgrading phpIPAM
to 1.8.1+ fixes that half at the source.

**`401 ... invalid permissions`** — app permission too low. Source needs
Read; master needs Read/Write.

**`Invalid request key <field>`** — that column exists on the source but
not the master. If it is a custom field, create it on the master with the
same name (step 2.2). Records not carrying it still sync.

**`Source instance has no section named 'X'`** — the exporter's
`sections:` does not match a section name on that instance. The message
lists what the API app can see; a section missing from that list usually
means the app lacks permission on it.

**`Target has no section named 'Site-A'`** — create it on the master
(step 2.1). Do not enable `create_missing_sections` to work around this:
a section created that way has no permissions and is invisible to
non-admins.

**`Sources 'x' and 'y' both target the section 'Z'`** — two subordinates
mapped to the same master section. Give each its own via `section_map`.
Never work around this.

**`Snapshot schema_version is N, this importer understands M`** — the two
sides are on different releases of this tool. The importer refuses rather
than guessing, because a format it does not understand may describe
records it would otherwise write wrongly.

**Upgrade the master first, then the subordinates.** A newer importer
still reads an older snapshot only if the version matches exactly, so the
practical sequence is: upgrade the master, upgrade one subordinate, let
it export, confirm the master applies it, then do the rest. The mirror
keeps delivering throughout — the snapshot in the repo is simply not
applied until the versions line up, and nothing is lost, because the
importer reads the tree at `HEAD` rather than replaying history.

Version 2 added L2 domains, VLANs and VRFs (the `_section.json` file in
each section directory) and tagged every document with a `kind`.

**`Checksum mismatch for ...`** — the snapshot was modified after export
or arrived corrupt. Re-run the export at that site and let the mirror
resend. Nothing is imported from a snapshot that fails verification.

**`git pull --ff-only failed`** — someone committed in the mirrored repo
on the receiving side. That side must never diverge; reset it to the
remote.

**`Refusing to run: ... over the safety limit`** — see Part 5.

**Every run rewrites the same records** — most likely custom-field
nesting is enabled on one side and not the other. Both API apps need
"Nest custom fields" set the same way.

**`phpipam-sync requires Python 3.9 or newer`** — the interpreter that
actually ran the script is too old. Note *where* this comes from: the
host may well have a new enough Python installed and still resolve
`python3` to an older one, which is why the cron entries above name the
interpreter by absolute path instead of relying on the shebang. Check
with `sudo -u ipamsync env -i /bin/sh -c 'command -v python3'` rather
than from your own shell, whose PATH is not the one cron uses.

---

## Checklists

### Per subordinate

- [ ] API enabled; HTTPS reachable
- [ ] API app `sync`, **Read**, SSL with App code, **Nest custom fields = Yes**
- [ ] `config.yml` names the right section; `verify_ssl: true`
- [ ] Token supplied via env or `token_command`, not in the config
- [ ] First export reviewed — counts sane, no unexpected dropped fields
- [ ] Custom field names noted for the master
- [ ] Repo pushes, and the mirror reaches the master
- [ ] Cron/timer with `flock`, output logged, alerting on non-zero

### Master

- [ ] One section per subordinate, created by hand
- [ ] Section + existing top-level subnets set read-only for user groups
- [ ] All custom fields from every subordinate created here, same names
- [ ] API app `sync`, **Read/Write**, SSL with App code, **Nest custom fields = Yes**
- [ ] Every mirrored repo cloned to its own directory
- [ ] `config.master.yml` — one source block per site, distinct sections
- [ ] Dry run reviewed per site; no cross-site mentions
- [ ] First load run by hand, per site
- [ ] Re-run confirmed to be a no-op
- [ ] Cron/timer with `flock`, alerting on non-zero
- [ ] Decision recorded on when to revisit `delete_drift`
