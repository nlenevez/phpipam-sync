# Example snapshot

A complete, real snapshot — exactly what crosses the git mirror. Not
hand-written: it is the actual output of `ipam_export.py` run against a
live phpIPAM 1.8.1 instance (the one `lab/setup.sh` builds), copied here
verbatim.

`tests/test_examples.py` reads and verifies this directory on every CI
run — checksums, schema version, and that the importer can build a plan
from it. So if the format changes and these are not regenerated, the
build fails. They cannot quietly go stale.

```
snapshot/
├── manifest.json
└── sections/
    └── shared/
        ├── 10.20.0.0_16.json        supernet, no addresses of its own
        ├── 10.20.5.0_24.json        nested, VLAN, custom fields, 3 addresses
        ├── 10.30.0.0_24.json        standalone /24, 1 address
        └── 2001-db8-5--_64.json     IPv6 /64
```

One file per subnet, so the git history shows *which* subnet changed, and
two subnets changing independently produce non-overlapping diffs.

## What each example demonstrates

### `manifest.json`

The index. Note:

- **`files`** — a SHA-256 per subnet file. The importer verifies all of
  them before touching the target: a one-way link gives the receiving
  side no way to ask for a resend, so a truncated transfer has to fail
  loudly rather than apply as if complete. The manifest is also
  authoritative about membership — a file on disk it does not list is
  stale and ignored.
- **`dropped_source_fields`** — empty here, meaning the field policy
  covered everything this phpIPAM returned. If your first export shows
  entries, read them: it is the fastest way to spot a version difference
  or a custom field that is not being carried.
- **`exported_at`** — when the content last **changed**, not when the
  exporter last ran. Unchanged data re-exports byte-identically and is
  not rewritten, which is what keeps a 5-minute cron from filling the
  repo with empty commits. It therefore cannot tell you the exporter is
  still alive; see DEPLOYMENT.md, Monitoring.
- **`source`** — provenance. Here it points at the lab instance on
  `127.0.0.1:8443`, since that is genuinely where these came from.

### `10.20.0.0_16.json` — a supernet with no addresses

`"addresses": []`. Utterly ordinary — most supernets have no addresses of
their own — and the case that exposed phpIPAM returning **404 for an
empty collection** rather than an empty list. See `lab/README.md`.

### `_section.json` — the VLANs and VRFs

One per section, holding its folders, L2 domains, VLANs and VRFs.

`folders` are paths — **lists** of names, not `"a/b"` strings, because a
phpIPAM folder name is free text and may contain a slash. They are sorted
shallowest-first, which is the order they have to be created in: phpIPAM
refuses a folder whose parent is not itself a folder.
 They live here
rather than on the subnets that use them because they replicate whether
or not anything references them — note `voice` and `CUST-B`, which
nothing points at, and `mgmt` on VLAN 9, which is there to prove numbers
sort numerically rather than as strings.

### `10.20.5.0_24.json` — the interesting one

- **`master_subnet: "10.20.0.0/16"`** — nesting recorded by the parent's
  **CIDR**, never its id. Database ids are meaningless across instances,
  so the target re-resolves the parent locally. This is the whole
  natural-key design in one field.
- **`vlan`** — a *reference* by natural key, `{"domain", "number"}`,
  never the VLAN's id. The VLAN itself is defined once in
  `_section.json`; the importer resolves this reference to the target's
  own id. `vrf` works the same way, keyed by name.
- **`fields.Owner` and `fields.custom_Notes`** — custom fields, stored
  **flat**. `Owner` has no prefix, which is what phpIPAM's UI actually
  produces; `custom_Notes` happens to have one. Both are carried because
  the exporter learns the real names from phpIPAM rather than guessing
  from a naming convention. Requires "Nest custom fields" on the API app.
- **`fields.tag: "2"`** — phpIPAM reads this as `tag` but only accepts
  `state` on **write**. The snapshot stores the *read* name so the format
  matches what the API reports; the importer translates at write time.
- **`addresses[].AssetTag`** — a custom field on an address, including
  `""` where it is unset, so clearing one upstream propagates.
- Addresses are in **numeric IP order** (`.2`, `.10`, `.20`, not
  lexical), so the diff of an address list reads in network order.

### `10.30.0.0_24.json` — a plain standalone subnet

`"master_subnet": null`, no VLAN. What most subnets look like.

### `2001-db8-5--_64.json` — IPv6

Same format throughout; only the slugged filename differs, since `:` and
`/` are awkward in paths. The file carries its own canonical
`"cidr": "2001:db8:5::/64"`, so the slug never has to be reversed.

## Things worth noticing about the format

**Every value is a string.** phpIPAM's API is loosely typed — the same
field comes back as `1`, `"1"` or `true` depending on version and
endpoint — so values are normalised on the way in. Without that, a
steady-state sync would "find" differences that do not exist and rewrite
every record on every run.

**Keys are sorted, indentation is fixed, there is a trailing newline.**
An unchanged export produces byte-identical files, which is what lets the
exporter commit only on a real change and makes the diffs readable.

**No database ids appear anywhere.** Not on subnets, not on addresses,
not on the section. That is deliberate and is the core safety property:
see the field policy in `ipamsync/model.py` for why each excluded field
is excluded.

## Regenerating these

```bash
cd lab && ./setup.sh
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
cd .. && ./ipam_export.py --config <a source config> --out-dir examples/snapshot
docker compose -f lab/docker-compose.yml down
```

Then check `tests/test_examples.py` still passes.
