# Verification lab

Two throwaway phpIPAM instances in Docker, so this tool can be checked
against real software instead of against a fake that agrees with it.

Covers both topologies: `verify.sh` for 1:1 replication (instances 1→2),
and `verify-fanin.sh` for many-to-one fan-in (instances 1 and 2 as
airgapped subordinates pushing up into instance 3 as the master).

```bash
cd lab
./setup.sh                                   # bring up + seed all three instances
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
export PHPIPAM_DST_TOKEN=DSTTOKEN0000000000000000000000
./verify.sh                                  # 1:1 -- 82 assertions
./verify-fanin.sh                            # fan-in -- 22 assertions
./verify-catchup.sh                          # mirror outage -- 14 assertions
docker compose down                          # destroy everything
```

The fan-in script's most important checks are that one subordinate's
import never reports or touches another's records, and that a config
which would put two subordinates in the same master section is refused
outright rather than silently letting one wipe the other.

`verify.sh` re-seeds by default, so it is safe to run repeatedly. Pass
`--no-setup` to check the lab in whatever state you have left it.

No volumes are declared anywhere, so `docker compose down` removes all
state. The TLS certificate is self-signed and generated fresh with a
two-day lifetime.

## Why the lab is shaped the way it is

**Two separate databases, not two sections of one.** The entire risk this
tool manages is that the two instances share no primary keys. A lab where
ids happened to line up would verify nothing, so `setup.sh` pushes
instance 2's auto-increment counters forward (sections from 500, subnets
from 900, addresses from 7000). `verify.sh` then asserts the ids really
did diverge before it draws any conclusion from them matching by CIDR.

**Instance 1's API app is read-only** (`app_permissions=1`). The exporter
is supposed to never write to the source. Giving it a read-only token
turns that from a claim into something phpIPAM enforces: if the exporter
ever attempted a write, the run would fail with a 401.

**nginx in front of both.** phpIPAM refuses app-code API authentication
over plain HTTP (`"SSL connection is required for API"`), so each
instance sits behind a TLS terminator that sets `X-Forwarded-Proto`,
trusted via `IPAM_TRUST_X_FORWARDED`. A useful side effect: the lab
exercises the `verify_ssl: false` path with a genuinely self-signed cert.

**A section that must not be replicated.** `setup.sh` creates a
`LocalOnly` section alongside `Shared`. Scoping that silently replicated
everything would otherwise pass every other check.

**A supernet with no addresses of its own.** This is what exposed
phpIPAM's 404-on-empty-collection behaviour (below). Ordinary-looking
data that happens to hit an edge case is worth more than data designed
around one.

### Custom fields, and an IPv6 subnet

`setup.sh` adds three custom fields — `Owner` and `AssetTag` (no prefix,
which is what phpIPAM's UI actually produces) and `custom_Notes` (with
one) — plus an IPv6 `/64` and its gateway. Both were previously listed as
"not covered", and both turned out to hide bugs.

## What running this against real phpIPAM found

Every one of these was invisible to a test suite built on a fake, because
the fake behaved the way phpIPAM's API *documentation* implies.

Verified on **1.8.1** and **1.7.4**; the first two behaviours are
identical on both, so neither fix is a version-specific workaround.

### 1. Empty collections are a 404, not an empty list

```
GET /api/sync/subnets/11/addresses/   ->  404 {"message": "No addresses found"}
GET /api/sync/sections/500/subnets/   ->  404 {"message": "No subnets found"}
```

This broke two entirely ordinary cases: exporting any supernet (which has
no addresses of its own), and the *first* import into a target section
(empty by definition). Handled in `ipamsync/client_ext.py`, which maps
the "No ... found" message to an empty list while still letting a genuine
"that subnet does not exist" 404 raise — treating a missing subnet as an
empty one would let the importer write records against an id that was
never there.

### 2. Subnets are read as `tag` but written as `state`

```
GET  /api/sync/subnets/12/       ->  {... "tag": 2 ...}
POST /api/sync/subnets/  {"tag": 2}    ->  400 "Invalid request key tag"
POST /api/sync/subnets/  {"state": 2}  ->  201 Subnet created
```

phpIPAM validates incoming keys against the `subnets` *table columns*
(where the field is `state`) but serialises it on read as `tag`. Every
subnet create and update failed until this was mapped. Addresses do not
have the problem — their controller accepts both — so the aliasing in
`ipamsync/model.py` is deliberately subnet-only.

The snapshot format stores the *read* name, so the on-disk format matches
what the API reports and is not contaminated by one instance's write
quirks; translation happens only at the point of writing.

### 3. Custom fields have no distinguishing name

phpIPAM decides what is custom by diffing the live table against its
shipped `SCHEMA.sql`. A custom field's column name is therefore whatever
the admin typed — a field created through the UI as "Owner" is a column
named `Owner`, indistinguishable in an API response from a phpIPAM field
this tool has not been taught about:

```
nesting off -> {... "Owner": "netops-team" ...}
nesting on  -> {... "custom_fields": {"Owner": "netops-team"} ...}
```

The original prefix-based detection (`custom_*`) only ever worked for
fields that happened to be named that way, so real custom fields were
silently reported as dropped instead of replicated. The fix reads
phpIPAM's own `custom_fields` block, which requires
`app_nest_custom_fields` on the API app — hence the setup instruction to
enable "Nest custom fields".

Note the read/write asymmetry again: reads nest, but **writes must be
flat**. Posting a nested object is rejected with `"Invalid request key
custom_fields"`. The snapshot stores them flat, matching the wire format
writes require.

### 4. An endless update loop on custom fields

The most consequential of these, and the least visible. With nesting
enabled, the *target* also returns custom fields nested — so comparing
the snapshot's flat `Owner` against a target record that had it under
`custom_fields` never matched. Every run therefore "found" a difference
and rewrote every custom field, forever:

```
update_subnet   10.20.5.0/24  [Owner='netops-team', custom_Notes='prefixed note']
update_address  10.20.5.0/24 10.20.5.2  [AssetTag='ASSET-0042']
```

Nothing was corrupt and no data was wrong — which is exactly why this
would have survived a casual look and then churned the target's change
history on every cron run, burying real changes in noise. Fixed by
flattening target records on read (`model.flatten_custom_fields`), and
covered by `test_flattened_target_compares_equal_to_the_snapshot` plus
the lab's step 6.

### 5. Overlap is validated on create, but not on update

```
POST  subnets/ {"subnet":"10.60.4.0","mask":"22","masterSubnetId":904}
  ->  409 "Subnet overlaps with 10.60.5.0/24"      (a sibling under 904)
PATCH subnets/905/ {"masterSubnetId":906}
  ->  200 Subnet updated                            (no validation at all)
PATCH subnets/905/ {"masterSubnetId":0}
  ->  200 Subnet updated                            (even with ancestors present)
```

Found by adding step 11 to `verify.sh`, and invisible to the unit suite,
whose fake has no concept of overlap. It matters because the ordinary
case of "someone inserted an intermediate aggregate upstream" hits it
head-on: the new `/22` cannot be created while the `/24` it will contain
is still a sibling under the same parent.

Because PATCH is *not* validated, the fix is an ordering one — detach
every subnet that is moving before creating anything, then re-attach.
See "Re-parenting" in `ipamsync/plan.py`.

The asymmetry cuts both ways: the same missing validation on PATCH means
phpIPAM will happily let the API build a hierarchy its own create path
would have rejected. This tool only ever writes parents that the source
already had, so it does not exploit that — but it is worth knowing before
trusting `masterSubnetId` from any other writer.

### 6. A folder is not a subnet, but lives in the same table

```
GET  sections/1/subnets/   ->  folders FIRST (`order by isFolder desc`),
                               subnet "0.0.0.0", mask null, name in `description`
GET  subnets/20/           ->  404 "No subnets found"     (20 is a folder)
POST folders/ {"isFolder":"1","masterSubnetId":<a subnet>}
                           ->  409 "Parent is not a folder"
```

Two consequences, both found by seeding folders into the lab:

- The custom-field probe reads `subnets[0]` to learn which columns are
  custom. Since phpIPAM sorts folders first and refuses to serve one from
  the single-subnet endpoint, that probe started hitting a folder and
  failing -- after which every unprefixed custom field on a record whose
  values were all null was reported as dropped instead of replicated. The
  data was still right; the *report* silently stopped being trustworthy.
- Folders may only nest under folders, so a folder tree is created
  shallowest-first and a subnet's parent is either a CIDR or a folder
  path, never both.

### 7. Deleting a folder deletes everything inside it

```
DELETE folders/900/   ->  200 "Subnet deleted"
```

...and the child folder, the subnets under it and all their addresses
are gone too, with nothing in the response to say so. Compare VLANs and
VRFs, which are the opposite:

```
DELETE vlans/300/  ->  200, and every subnet's vlanId is set to NULL
DELETE vrfs/60/    ->  200, and every subnet's vrfId  is set to NULL
                       -- the subnets themselves survive
```

So the two need opposite handling. A VLAN or VRF can be deleted whenever
it is orphaned. A folder cannot: the importer re-reads it first and
refuses to delete one that still holds records, because a single planned
deletion would otherwise destroy an unbounded number of unplanned ones —
and count as one against the safety limit while doing it.

### What the failure looked like, and why it was contained

The second bug produced this, which is the error handling behaving
exactly as designed:

```
ERROR  create_subnet 10.20.0.0/16: POST subnets/ failed: Invalid request key tag
ERROR  create_address 10.20.5.0/24 10.20.5.2: Subnet 10.20.5.0/24 not found in
       target section id 500 while applying.
applied 0 change(s), 7 error(s)
```

Nothing was corrupted. The confirm-by-re-read in `ipamsync/target.py`
noticed the subnet had not actually been created and refused to write
addresses into it, and per-record isolation meant the run reported all
seven failures at once instead of dying on the first.

## Running against a different version

The image tag is pinned to `v1.8.1` but overridable, because the bugs
above were all version-specific API behaviours — being able to re-verify
against whatever you actually run matters more than testing one blessed
version:

```bash
PHPIPAM_VERSION=v1.7.4 ./verify.sh
```

Verified on 1.8.1 and 1.7.4.

## What the lab still does not prove

Other phpIPAM versions may differ in exactly the ways found above. The
`dropped_source_fields` report in the manifest is the thing to read after
a first export against your own instances: it names every field the
source returned that the tool did not carry.

Not covered: instances with custom `ipTags` (see `options.sync_tags` in
the main README) and phpIPAM's permissions model.
