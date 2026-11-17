# Deployment guide

Setup for the fan-in topology: several airgapped phpIPAM instances, each
owning its own networks, replicating into one read-only master over
one-way git mirroring. For 1:1, follow Part 1 for the source and Part 2
for the replica with a single source.

```
  SUBORDINATE (×N, airgapped)              MASTER

  phpIPAM ──export──▶ site-a-ipam.git ─mirror─▶ site-a-ipam.git ──import──▶ phpIPAM
  (owns its networks)                                                       (read-only
                                                                             to users)
```

Targets phpIPAM **1.8.1** only; for anything else see
[Before you start](#before-you-start).

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
10. [Upgrading across a schema change](#upgrading-across-a-schema-change)
11. [Runbook](#runbook)
12. [Checklists](#checklists)

---

## Before you start

**Requirements on every host running the tool**

- Python 3.9+, `requests`, `PyYAML` (`pip install -r requirements.txt`;
  vendored copies are used if not installed). Check the interpreter cron
  will use (Part 4). The tool refuses to start on anything older.
- `git`, with the mirrored repo cloned locally.
- **HTTPS to the phpIPAM instance.** phpIPAM refuses app-code API
  authentication over plain HTTP with
  `503 "SSL connection is required for API"`. Behind a reverse proxy,
  set `IPAM_TRUST_X_FORWARDED=true` (or `$trust_x_forwarded_headers`)
  so phpIPAM honours `X-Forwarded-Proto`.

**Enable the API** on every instance: Administration → phpIPAM settings →
API = On. Otherwise every call returns `503 "API server disabled"`.

**Running a version other than 1.8.1?** Run the lab against it first:

```bash
cd lab
PHPIPAM_VERSION=v1.8.1 ./setup.sh     # the version this targets
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
export PHPIPAM_DST_TOKEN=DSTTOKEN0000000000000000000000
./verify.sh && ./verify-fanin.sh
docker compose down
```

If they fail, the output names what differs. Do not deploy until it is
understood.

---

## Part 1 — each subordinate

Repeat for every site.

> **If the data currently exists only on the master** and these instances
> are empty, seed each one first with [FILL.md](FILL.md), then come back.

### 1.1 Create the API app (read-only)

Administration → API → Create API app:

| Field | Value |
|---|---|
| App id | `sync` |
| App security | **SSL with App code** |
| App permissions | **Read** |
| Nest custom fields | **Yes** |

The generated **App code** is the token.

**Read** makes phpIPAM enforce that the exporter never writes (a write
would fail with 401).

**Nest custom fields = Yes** is required if you use custom fields.
Without it they are indistinguishable from unknown phpIPAM fields and
are reported in `dropped_source_fields` instead of carried.

### 1.2 Decide what to replicate

The tool replicates **whole sections**: the one your networks are in, or
a dedicated one. The name goes in `sections:` below; the master maps it
to that site's own section.

### 1.3 Install

```bash
sudo install -d -o ipamsync -g ipamsync /opt/phpipam-sync /srv/ipam-data
git clone <this repo> /opt/phpipam-sync
cd /opt/phpipam-sync && pip install -r requirements.txt

# the repo that mirrors up to the master
git clone git@<this-site-git-server>:ipam/site-a-ipam.git /srv/ipam-data
```

### 1.4 Configure

`/opt/phpipam-sync/config.yml`, from `config.example.yml`:

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

Supply the token with `token_env`, or `token_command` for a secret
store:

```yaml
  token_command: "ansible-vault view /etc/phpipam-sync/token"
```

### 1.5 First export

```bash
export PHPIPAM_SRC_TOKEN='<app code>'
cd /opt/phpipam-sync
./ipam_export.py --config config.yml --out-dir /srv/ipam-data
```

[`examples/`](examples/) shows an annotated real snapshot to compare
against.

Read the output:

- **`custom subnet field(s): ...`**: custom fields found. Note the names;
  the master needs the same fields.
- **`note: ... field(s) present on the source but not replicated`**: if
  it mentions nesting, turn on "Nest custom fields" and re-run.
  Otherwise the named fields are ones this tool does not carry; check
  whether you need them.
- **Subnet/address counts**: compare with the UI.

Then commit and push:

```bash
./ipam_export.py --config config.yml --out-dir /srv/ipam-data --commit --push
```

### 1.6 Confirm the mirror

On the master, confirm the repo arrived and the commit matches before
configuring the master side.

---

## Part 2 — the master

### 2.1 Create one section per subordinate

Administration → Sections → Create:

| Section | Description |
|---|---|
| `Site-A` | Mirrored from site A |
| `Site-B` | Mirrored from site B |
| … | one per subordinate |

**Each subordinate must have its own section.** Each import treats
anything in its target section that is absent from its snapshot as
drift, and deletes it once strict mirror is on; a shared section means
one site's sync deletes another's subnets. The tool refuses to start if
two sources target the same section.

**Turn OFF "strict mode" on each replicated section** (on by default).
With it on, phpIPAM refuses a created subnet that overlaps a sibling, so
an aggregate added upstream above existing *top-level* subnets fails
with `409 ... overlaps with ...` (the importer reorders what it can; see
"Re-parenting" in `ipamsync/plan.py`). Safe for sections only this tool
writes: the source already validated the hierarchy. On 1.8.1 the same
create returns `409` with strict mode on and `201` with it off. It is
per-section. The overlap check is also VRF-aware, so subnets overlapping
in different VRFs are refused until their VRF assignment has replicated.

Set each section's permissions to **read** for user groups (2.5).

### 2.2 Create the custom fields

**Custom fields are schema, not data.** The tool replicates values; it
cannot create fields. Every custom field used by any subordinate must
exist on the master with the **same name**, or each record carrying it
is rejected:

```
create_subnet 10.20.0.0/16: POST subnets/ failed: Invalid request key Owner
  ('Owner' exists on the source instance but not on this one. If it is a
  custom field, create it here too under Administration > Custom fields...)
```

Administration → Custom fields: add the fields from step 1.5, same
names, for `subnets` and `addresses` as needed.

### 2.3 Create the API app (read-write)

Administration → API → Create API app:

| Field | Value |
|---|---|
| App id | `sync` |
| App security | **SSL with App code** |
| App permissions | **Read / Write** |
| Nest custom fields | **Yes** |

With nesting off here, the master returns custom fields in a different
shape from the snapshot and every custom field is rewritten on every run.

### 2.4 Clone every mirrored repo

```bash
sudo install -d -o ipamsync -g ipamsync /srv/ipam
for site in a b c d e; do
  git clone git@<master-git-server>:ipam/site-$site-ipam.git /srv/ipam/site-$site-ipam
done
```

One directory per subordinate; two sources may not share one.

### 2.5 Make the master read-only to users

Group permissions gate the UI; the API app's own permission governs the
importer. With every subnet read-only for all groups, the importer still
creates and updates normally.

- **New subnets inherit their parent's permissions**, so subnets created
  later come out read-only too.
- **A nested subnet inherits from its parent *subnet*, not the section.**
  Set the permission on **the section and every existing top-level
  subnet in it.**

The importer never touches `permissions`. You still need write access
yourself to action the drift report.

### 2.5b Read-only to administrators as well (optional)

phpIPAM cannot make data read-only to an Administrator. Both permission
checks open with:

```php
// functions/classes/class.Sections.php:456, class.Subnets.php:3025
# if user is admin then return 3, otherwise check
if($user->role == "Administrator")	{ return 3; }
```

Level 3 is read/write/admin, returned before any permission lookup.
There is no lock flag or override an administrator does not outrank, so
it has to be enforced in the database.

The importer writes through the same phpIPAM code an administrator uses,
so the database must tell them apart: two MySQL users, two phpIPAM
configurations, one database:

| | reached by | MySQL user | rights |
|---|---|---|---|
| **Private instance** | the importer only — bind to localhost or a management VLAN | `ipam_sync` | full DML |
| **Public instance** | people | `ipam_ro` | restricted, below |

Same phpIPAM release, same schema; only `config.php` differs. The
importer's API app lives on the private one.

#### Option A — table grants (whole master read-only)

For a master holding only replicated data:

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
grant; an administrator's edit gets a database error. Without the
`lastLogin` column grant every login shows a red error banner.

#### Option B — triggers (per-section granularity)

Only if the master also holds locally-editable sections:

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

MySQL 8 details:

- **Test `SESSION_USER()`, not `CURRENT_USER()`.** Triggers run as their
  definer, so `CURRENT_USER()` reports the definer and the exemption
  never matches.
- **`SIGNAL ... SET MESSAGE_TEXT` takes no expression.** Build the
  message in a declared variable; inline `CONCAT(...)` is a syntax
  error.

Needed on `subnets` and `ipaddresses`, for INSERT, UPDATE and DELETE.

#### Limits and costs

Anyone with MySQL credentials can still drop a trigger or re-grant; this
prevents accidental UI edits, not deliberate ones.

- Failures surface as raw database errors.
- phpIPAM upgrades run DDL: run them on the private instance as the
  privileged user.
- Custom-field management does `ALTER TABLE`, so it is unavailable on
  the public instance.

**Recommendation:** for a purely replicated master, Option A, no
day-to-day Administrator accounts on the public instance, one
break-glass account. Option B only for local sections alongside
mirrored ones.

**Untested as a whole.** The phpIPAM behaviour is from 1.8.1's source
and the two MySQL details were reproduced on MySQL 8, but the two-config
split has not been run in the lab. Prove it on a copy first.

### 2.6 Configure

`/opt/phpipam-sync/config.master.yml`, from `config.master.example.yml`:

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

Writes nothing. Check per site:

- Each `[site-x]` block mentions only that site's networks. Otherwise
  the sectioning is wrong; fix it before applying.
- `create_subnet` / `create_address` counts match that subordinate's
  export.
- `ERROR ... Invalid request key <field>` means a missing custom field
  (2.2).

---

## Part 3 — first load

**By hand**, not from cron: one POST per record, ~**43 writes/s**, so
~16 minutes per 40,000 records (longer over a network).

```bash
./ipam_import.py --config config.master.yml --pull --apply --source site-a
```

One site at a time; check the master's UI between sites.

Then confirm a re-run is a no-op:

```bash
./ipam_import.py --config config.master.yml --pull --apply
# ... every site should say: changes: none -- target already matches the snapshot
```

If it is not, stop: something is rewritten on every run.

---

## Part 4 — automate

### On each subordinate

```cron
*/5 * * * * ipamsync /usr/bin/flock -n /var/lock/ipam-export.lock \
  /usr/bin/python3.12 /opt/phpipam-sync/ipam_export.py \
  --config /opt/phpipam-sync/config.yml \
  --out-dir /srv/ipam-data --commit --push >> /var/log/ipam-export.log 2>&1
```

Export only commits on change, so a 5-minute interval adds no empty
commits.

**Name the interpreter by absolute path**, not the
`#!/usr/bin/env python3` shebang: cron's PATH can resolve `python3` to
an older version. Use `command -v python3.12` on that host.

### On the master

```cron
*/10 * * * * ipamsync /usr/bin/flock -n /var/lock/ipam-import.lock \
  /usr/bin/python3.12 /opt/phpipam-sync/ipam_import.py \
  --config /opt/phpipam-sync/config.master.yml \
  --pull --apply --quiet-drift >> /var/log/ipam-import.log 2>&1
```

**`flock` is required**: there is no internal locking. `-n` skips a run
while the previous one is still going.

Get the token to the job with an `EnvironmentFile` (systemd timer) or
`token_command`; never in the crontab.

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
subordinates deleted, and over-reports utilisation.

First run additive for a few weeks and read the drift reports (drop
`--quiet-drift`). Site A's drift must only mention site A's networks;
anything else means the sectioning is wrong.

Then:

```bash
# what would be deleted -- writes nothing
./ipam_import.py --config config.master.yml --pull --delete

# then, once you agree with the list
./ipam_import.py --config config.master.yml --pull --delete --apply
```

The first strict run deletes everything accumulated while additive. Then
set `delete_drift: true`. Strict mirror also removes anything created by
hand on the master.

**What deletion covers:**

| Object | Deleted when absent from the snapshot? |
|---|---|
| Subnet, address | Yes — the section is owned outright by one source |
| Folder | Yes, but only once **empty** (see below) |
| VLAN in a domain scoped to this section alone | Yes |
| VLAN in the `default` domain | **Never** — domain 1 is implicitly in every section |
| VLAN in a domain shared by several sections | **Never** |
| VRF scoped to this section alone | Yes |
| VRF shared with another section | **Never** — removing it would take it from that section too |
| L2 domain itself | Never — they are containers, and shared by nature |

**Put each site's VLANs in that site's own L2 domain**, or they
accumulate on the master and are never cleaned up. Default-domain VLANs
are reported as drift but never removed.

**Folders are only deleted once empty.** phpIPAM deletes a folder's
entire contents with it (child folders, their subnets, all addresses),
reporting only `Subnet deleted`. The importer re-reads each folder first
and skips any that still holds records; a later run removes it once its
contents are gone.

A run deleting more than `delete_limit_fraction` (default 10%, floor 5
records) of the in-scope records is refused, writing nothing:

```
Refusing to run: this would delete 61 of 69 in-scope record(s) on the target
(88%), which is over the safety limit of 6 (10%, floor 5).
This usually means the snapshot is empty, truncated, or was exported from the
wrong section -- NOT that this much really was deleted upstream.
```

`--force-delete` overrides it for one run. Find the cause first; it is
usually a bad snapshot.

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

**The master cannot detect that a subordinate has stopped exporting.**
It keeps applying the last snapshot and reporting success; `exported_at`
means "content last changed", not "the exporter ran".

1. **Monitor the export job on each subordinate** (cron exit status).
2. **`--stale-after-days`** (default 30) warns when a site's snapshot
   content has not changed in that long. A static site trips it too.

### Watch for

- `applied N change(s)` unexpectedly large in steady state: something is
  rewritten every run.
- The same record erroring every run (it alerts every cycle until fixed).
- Drift growing without bound while additive.

---

## When the mirror fails and then catches up

The importer reads the tree at `HEAD`, not the history, so a backlog
collapses into one reconciliation sized by the **net difference**.
Measured figures (14-day outage, one run, 17 s): see
[README](README.md#recovering-from-a-mirror-outage).

- **Nothing is replayed.** A host renamed five times is written once.
- **Churn that cancelled out costs nothing**: an address created on day
  3 and deleted on day 9 is not in the plan.
- **Bounded by a full reload**: ~43 writes/s, ~16 minutes per 40,000
  records. A fortnight touching a few per cent of the estate takes a
  minute or two.
- **`flock -n`** stops a long catch-up overlapping the next cron tick.
- **After a long outage, run it by hand first** and read the plan
  (`--pull` without `--apply`).

### Mass deletion during the outage

With `delete_drift` on, a backlog of upstream deletions trips the safety
limit and the whole run is refused, target untouched:

```
Refusing to run: this would delete 1847 of 2004 in-scope record(s) on the
target (92%), which is over the safety limit of 200 (10%, floor 5).
```

A long outage and a mis-scoped snapshot look the same from the target.
Confirm the deletions are genuine (check the source and `manifest.json`),
then re-run with `--force-delete`.

### Audit trail

phpIPAM's history on the master shows one batch at catch-up. The git
repo has every intermediate commit, per subnet; do not truncate history
you may need.

## Repository growth and maintenance

Measured growth: see [README](README.md#repository-growth).
`lab/measure-growth.py` drives the real `write_snapshot()`; the
low-churn case is 371 MB uncompressed across 10,780 blobs, packed to
2.3 MB. Re-run it with your own figures:

```bash
./lab/measure-growth.py --subnets 400 --per-subnet 120 \
                        --changes-per-day 500 --days 365
```

### Routine maintenance

None required: git's automatic housekeeping packs loose objects, and
most git servers run their own. Optional monthly `git gc`:

```cron
17 3 1 * * ipamsync git -C /srv/ipam-data gc --quiet --prune=now
```

**The master can clone shallow** (592 KB vs 2.7 MB on the simulated
repo); it only reads the current tree:

```bash
git clone --depth 1 <url> /srv/ipam/site-a-ipam
```

`git pull --ff-only` (what `--pull` runs) works on a shallow clone and
keeps the boundary, but accumulates pulled commits. To re-shallow:

```bash
git -C /srv/ipam/site-a-ipam fetch --depth=1 origin main
git -C /srv/ipam/site-a-ipam reset --hard origin/main
git -C /srv/ipam/site-a-ipam reflog expire --expire=now --all
git -C /srv/ipam/site-a-ipam gc --prune=now
```

### What keeps it small (don't break these)

- **The exporter commits only on a real change**; unchanged data
  re-exports byte-identically. A timestamp in `manifest.json` would mean
  a commit every run (~288 a day at 5 minutes); a regression test guards
  this.
- **Canonical serialisation** (sorted keys, fixed indent, numeric IP
  order).
- **One file per subnet.**

### Truncating history

**Rewriting history breaks the mirror**: it needs a force push and every
consumer must re-clone. Least disruptive form, a fresh root commit:

```bash
# On the SUBORDINATE, after confirming the master is fully caught up.
cd /srv/ipam-data
git checkout --orphan truncated
git add -A
git commit -m "snapshot history restarted $(date -I)"
git branch -M truncated main
git push --force origin main
```

On the master, delete and re-clone that site's directory, then run the
importer **without `--apply`**: the plan must be empty. If not, stop.

Do not do this with `delete_drift` on until the master's copy is
re-cloned: a half-transferred history can look like a mass deletion.

---

## Upgrading across a schema change

The snapshot carries a `schema_version`; the importer refuses a version
it does not recognise.

| Version | What it added |
|---|---|
| 1 | sections, subnets, addresses |
| 2 | `kind` on every document; `_section.json` per section holding L2 domains, VLANs and VRFs; VLAN/VRF references on subnets |
| 3 | `folders` in the section document, `parent_folder` on subnets |

### Why it must be lockstep

An older exporter writes no `folders`; a newer importer would read that
as "this section has no folders" and, with strict mirror on, **delete
every folder on the master**. The version check prevents a mixed pair
from applying.

### Order

**Upgrade the master first, then each subordinate.** Each site pauses
until both ends match; nothing is lost, and a site upgraded later
catches up in one run (see
[When the mirror fails and then catches up](#when-the-mirror-fails-and-then-catches-up)).

```bash
# 1. master
cd /opt/phpipam-sync && git pull && pip install -r requirements.txt
./ipam_import.py --config config.master.yml --pull      # dry run: expect a
                                                        # schema_version error
                                                        # per site, nothing tried

# 2. one subordinate, then confirm the master applies it
cd /opt/phpipam-sync && git pull && pip install -r requirements.txt
./ipam_export.py --config config.yml --out-dir /srv/ipam-data --commit --push

# on the master -- this site should now plan normally
./ipam_import.py --config config.master.yml --source site-a --pull

# 3. the rest
```

### Before the first version 3 import

- **An L2 domain per section** (Administration → VLAN → L2 domains),
  scoped to that site's section. VLANs left in `default` replicate but
  are never deleted (see [Part 5](#part-5--switching-to-strict-mirror-later)).
- **Strict mode OFF** on each replicated section (2.1).

### Turn strict mirror off for the upgrade

If `delete_drift` is on, set it to `false` until every site is upgraded
and has run cleanly once, so the first import of folders, VLANs and VRFs
can be reviewed as creates. Turn it back on afterwards.

### Afterwards

Per site:

```bash
./ipam_import.py --config config.master.yml --source site-a --pull
# expect: changes: none
```

---

## Runbook

**`503 "API server disabled"`**: enable the API in Administration →
phpIPAM settings.

**`503 "SSL connection is required for API"`**: app-code auth needs
HTTPS. Behind a proxy, set `IPAM_TRUST_X_FORWARDED=true` and forward
`X-Forwarded-Proto`.

**`415 "Invalid Content type <value>"`**: phpIPAM accepts
`application/json`, `application/xml`,
`application/x-www-form-urlencoded`, an absent header, and an empty one.
This tool always sends `application/json`, so something in between is
supplying or rewriting the header: nginx, `fastcgi_param CONTENT_TYPE`,
a reverse proxy, or a WAF.

The message ends with the offending value. **If nothing seems to follow
it**, the value is whitespace-only, or empty on phpIPAM ≤ 1.7.x (below).
Log what arrives in nginx:

```nginx
log_format ct '$remote_addr "$request" $status ct="$http_content_type"';
access_log /var/log/nginx/ct.log ct;
```

and look for anything setting it:
`grep -rniE 'content.type|fastcgi_param CONTENT_TYPE' /etc/nginx/`.

Confirm from the phpIPAM host itself (bypasses any outbound proxy):

```bash
curl -sk -H "token: $TOKEN" https://127.0.0.1/api/<app_id>/sections/ | head -c 200
curl -sk -H "token: $TOKEN" -H 'Content-Type: application/json' \
     https://127.0.0.1/api/<app_id>/sections/ | head -c 200
```

**phpIPAM ≤ 1.7.x on PHP 8** also refuses an *empty* value: the check
reads `strlen(@$ct==0)`, fixed to `strlen(@$ct)==0` in 1.8.1. PHP 7
evaluates it as `strlen(true)` → 1 and passes everything; PHP 8 makes
`"" == 0` false. Those versions break on a **PHP** upgrade. phpIPAM
1.8.1+ fixes it.

**`401 ... invalid permissions`**: app permission too low. Source needs
Read; master needs Read/Write.

**`Invalid request key <field>`**: the column exists on the source but
not the master. If it is a custom field, create it on the master with
the same name (2.2). Records without it still sync.

**`Source instance has no section named 'X'`**: `sections:` does not
match a section on that instance. The message lists what the API app
can see; a missing section usually means the app lacks permission on it.

**`Target has no section named 'Site-A'`**: create it on the master
(2.1). Do not enable `create_missing_sections` instead: a section
created that way has no permissions and is invisible to non-admins.

**`Sources 'x' and 'y' both target the section 'Z'`**: two subordinates
mapped to one master section. Give each its own via `section_map`. Never
work around this.

**`Snapshot schema_version is N, this importer understands M`**: the two
sides run different releases. Expected mid-upgrade; see
[Upgrading](#upgrading-across-a-schema-change).

**`Checksum mismatch for ...`**: the snapshot was modified after export
or arrived corrupt. Re-run the export at that site. Nothing is imported
from a snapshot that fails verification.

**`git pull --ff-only failed`**: someone committed in the mirrored repo
on the receiving side. Reset it to the remote.

**`Refusing to run: ... over the safety limit`**: see Part 5.

**Every run rewrites the same records**: usually "Nest custom fields"
differs between the two API apps.

**`phpipam-sync requires Python 3.9 or newer`**: the interpreter that ran
the script is too old, even if a newer one is installed. Name it by
absolute path in cron (Part 4). Check what cron gets with
`sudo -u ipamsync env -i /bin/sh -c 'command -v python3'`.

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
- [ ] Strict mode off on each replicated section
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
