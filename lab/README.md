# Verification lab

Three throwaway phpIPAM instances in Docker. `verify.sh` tests 1:1
replication (instance 1 → 2); `verify-fanin.sh` tests fan-in (instances
1 and 2 as subordinates, instance 3 as master); `verify-catchup.sh`
tests recovery from a mirror outage.

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

`verify-fanin.sh` checks that one subordinate's import never reports or
touches another's records, and that a config putting two subordinates in
one master section is refused.

The verify scripts re-seed by default; `--no-setup` checks the lab as it
stands.

No volumes are declared, so `docker compose down` removes all state. The
TLS certificate is self-signed, generated fresh, valid two days.

## Lab design

- **Separate databases, diverged ids.** `setup.sh` pushes instance 2's
  auto-increment counters forward (sections from 500, subnets from 900,
  addresses from 7000); `verify.sh` asserts the ids really differ.
- **Instance 1's API app is read-only** (`app_permissions=1`), so an
  exporter write would fail with 401.
- **nginx in front of each instance** terminates TLS and sets
  `X-Forwarded-Proto` (trusted via `IPAM_TRUST_X_FORWARDED`), since
  phpIPAM refuses app-code auth over plain HTTP. This also exercises
  `verify_ssl: false`.
- **A `LocalOnly` section** alongside `Shared`, which must not replicate.
- **A supernet with no addresses of its own** (quirk 1).
- **Custom fields** `Owner` and `AssetTag` (no prefix, as the UI creates
  them) and `custom_Notes` (prefixed), plus an IPv6 `/64` and its
  gateway.

## phpIPAM 1.8.1 API quirks

### 1. Empty collections are a 404, not an empty list

```
GET /api/sync/subnets/11/addresses/   ->  404 {"message": "No addresses found"}
GET /api/sync/sections/500/subnets/   ->  404 {"message": "No subnets found"}
```

Affects exporting any supernet and the first import into an empty
section. `ipamsync/client_ext.py` maps the "No ... found" message to an
empty list, while a 404 for a subnet that does not exist still raises.

### 2. Subnets are read as `tag` but written as `state`

```
GET  /api/sync/subnets/12/       ->  {... "tag": 2 ...}
POST /api/sync/subnets/  {"tag": 2}    ->  400 "Invalid request key tag"
POST /api/sync/subnets/  {"state": 2}  ->  201 Subnet created
```

phpIPAM validates incoming keys against the `subnets` table columns
(`state`) but serialises the field as `tag`. Addresses accept both, so
the alias in `ipamsync/model.py` is subnet-only. The snapshot stores the
read name; translation happens at write time.

### 3. Custom fields have no distinguishing name

phpIPAM decides what is custom by diffing the live table against its
shipped `SCHEMA.sql`, so a custom field's column is whatever the admin
typed:

```
nesting off -> {... "Owner": "netops-team" ...}
nesting on  -> {... "custom_fields": {"Owner": "netops-team"} ...}
```

The exporter reads phpIPAM's `custom_fields` block, which requires
`app_nest_custom_fields` ("Nest custom fields") on the API app.

Reads nest, but **writes must be flat**: posting a nested object is
rejected with `"Invalid request key custom_fields"`. The snapshot stores
them flat.

### 4. Custom fields also come back nested from the target

Comparing the snapshot's flat `Owner` with a target record holding it
under `custom_fields` never matches, so every run rewrites every custom
field:

```
update_subnet   10.20.5.0/24  [Owner='netops-team', custom_Notes='prefixed note']
update_address  10.20.5.0/24 10.20.5.2  [AssetTag='ASSET-0042']
```

Target records are flattened on read (`model.flatten_custom_fields`).
Covered by `test_flattened_target_compares_equal_to_the_snapshot` and
step 6 of `verify.sh`.

### 5. Overlap is validated on create, but not on update

```
POST  subnets/ {"subnet":"10.60.4.0","mask":"22","masterSubnetId":904}
  ->  409 "Subnet overlaps with 10.60.5.0/24"      (a sibling under 904)
PATCH subnets/905/ {"masterSubnetId":906}
  ->  200 Subnet updated                            (no validation at all)
PATCH subnets/905/ {"masterSubnetId":0}
  ->  200 Subnet updated                            (even with ancestors present)
```

Inserting an intermediate aggregate upstream hits this: the new `/22`
cannot be created while the `/24` it will contain is still its sibling.
The importer detaches every moving subnet before creating anything, then
re-attaches ("Re-parenting" in `ipamsync/plan.py`). Tested by step 11
of `verify.sh`.

Unvalidated PATCH also means the API accepts hierarchies the create path
would reject. This tool only writes parents the source already had; do
not trust `masterSubnetId` from other writers.

### 6. A folder is not a subnet, but lives in the same table

```
GET  sections/1/subnets/   ->  folders FIRST (`order by isFolder desc`),
                               subnet "0.0.0.0", mask null, name in `description`
GET  subnets/20/           ->  404 "No subnets found"     (20 is a folder)
POST folders/ {"isFolder":"1","masterSubnetId":<a subnet>}
                           ->  409 "Parent is not a folder"
```

- Custom-field discovery probes `subnets[0]`. With folders listed first
  and unservable from the single-subnet endpoint, the probe fails and
  unprefixed custom fields whose values are all null are reported as
  dropped. The probe skips folders.
- Folders nest only under folders: folder trees are created
  shallowest-first, and a subnet's parent is a CIDR or a folder path,
  never both.

### 7. Deleting a folder deletes everything inside it

```
DELETE folders/900/   ->  200 "Subnet deleted"
```

The child folder, its subnets and all their addresses are deleted too,
unreported. VLANs and VRFs are the opposite:

```
DELETE vlans/300/  ->  200, and every subnet's vlanId is set to NULL
DELETE vrfs/60/    ->  200, and every subnet's vrfId  is set to NULL
                       -- the subnets themselves survive
```

A VLAN or VRF is deleted when orphaned. A folder is re-read first and
not deleted while it holds records; otherwise one planned deletion would
remove an unbounded number of unplanned ones and count once against the
safety limit.

### Failure containment

Quirk 2 before it was handled:

```
ERROR  create_subnet 10.20.0.0/16: POST subnets/ failed: Invalid request key tag
ERROR  create_address 10.20.5.0/24 10.20.5.2: Subnet 10.20.5.0/24 not found in
       target section id 500 while applying.
applied 0 change(s), 7 error(s)
```

The confirm-by-re-read in `ipamsync/target.py` saw the subnet had not
been created and refused to write addresses into it; per-record
isolation reported all seven failures in one run.

## Running against a different version

The image tag is pinned to `v1.8.1` and overridable:

```bash
PHPIPAM_VERSION=v1.9.0 ./verify.sh          # any published tag
```

## What the lab does not prove

Other phpIPAM versions may differ in the ways above. After a first
export against your own instances, read `dropped_source_fields` in the
manifest: every field the source returned that was not carried.

Not covered: instances with custom `ipTags` (see `options.sync_tags` in
the main README) and phpIPAM's permissions model.
