# Example snapshot

A complete snapshot, exactly what crosses the git mirror: unedited
output of `ipam_export.py` against phpIPAM 1.8.1 (the instance
`lab/setup.sh` builds).

`tests/test_examples.py` verifies this directory on every CI run
(checksums, schema version, and that the importer can build a plan from
it), so a format change without regenerating these fails the build.

```
snapshot/
├── manifest.json
└── sections/
    └── shared/
        ├── _section.json            folders, L2 domains, VLANs, VRFs
        ├── 10.20.0.0_16.json        supernet, no addresses of its own
        ├── 10.20.5.0_24.json        nested, VLAN, custom fields, 3 addresses
        ├── 10.30.0.0_24.json        standalone /24, 1 address
        └── 2001-db8-5--_64.json     IPv6 /64
```

One file per subnet, so the git history shows which subnet changed.

## What each example demonstrates

### `manifest.json`

- **`files`**: a SHA-256 per subnet file, all verified before the
  importer touches the target. The manifest is authoritative about
  membership: a file it does not list is ignored.
- **`dropped_source_fields`**: empty here, meaning the field policy
  covered everything this phpIPAM returned. Entries on your first export
  point at a version difference or an uncarried custom field.
- **`exported_at`**: when the content last **changed**, not when the
  exporter last ran. Unchanged data re-exports byte-identically and is
  not rewritten, so this is not a liveness signal; see DEPLOYMENT.md,
  Monitoring.
- **`source`**: provenance; here the lab instance on `127.0.0.1:8443`.

### `10.20.0.0_16.json` — a supernet with no addresses

`"addresses": []`. The case where phpIPAM returns **404 for an empty
collection**; see `lab/README.md`.

### `_section.json` — folders, L2 domains, VLANs and VRFs

One per section.

`folders` are paths: **lists** of names, not `"a/b"` strings, since a
folder name may contain a slash. Sorted shallowest-first, the order they
must be created in (phpIPAM refuses a folder whose parent is not a
folder).

VLANs and VRFs live here rather than on the subnets that use them
because they replicate whether or not anything references them: `voice`
and `CUST-B` are unreferenced, and `mgmt` on VLAN 9 shows numbers sort
numerically, not as strings.

### `10.20.5.0_24.json` — the interesting one

- **`master_subnet: "10.20.0.0/16"`**: nesting recorded by the parent's
  **CIDR**, never its id; the target resolves the parent locally.
- **`vlan`**: a reference by natural key, `{"domain", "number"}`, never
  the VLAN's id. The VLAN is defined in `_section.json`. `vrf` works the
  same way, keyed by name.
- **`fields.Owner` and `fields.custom_Notes`**: custom fields, stored
  **flat**. `Owner` has no prefix (as the UI creates it); `custom_Notes`
  has one. The exporter learns the names from phpIPAM, which requires
  "Nest custom fields" on the API app.
- **`fields.tag: "2"`**: read as `tag`, written as `state`. The snapshot
  stores the read name; the importer translates at write time.
- **`addresses[].AssetTag`**: a custom field on an address, `""` where
  unset, so clearing one upstream propagates.
- Addresses are in **numeric IP order** (`.2`, `.10`, `.20`).

### `10.30.0.0_24.json` — a plain standalone subnet

`"master_subnet": null`, no VLAN.

### `2001-db8-5--_64.json` — IPv6

Same format; only the filename is slugged, since `:` and `/` are awkward
in paths. The file carries its canonical `"cidr": "2001:db8:5::/64"`, so
the slug is never reversed.

## Format properties

- **Every value is a string**, normalised from phpIPAM's loosely typed
  API so a steady-state sync finds no false differences.
- **Sorted keys, fixed indentation, trailing newline**: an unchanged
  export is byte-identical, so the exporter commits only on real change.
- **No database ids anywhere.** See the field policy in
  `ipamsync/model.py` for why each excluded field is excluded.

## Regenerating these

```bash
cd lab && ./setup.sh
export PHPIPAM_SRC_TOKEN=SRCTOKEN0000000000000000000000
cd .. && ./ipam_export.py --config <a source config> --out-dir examples/snapshot
docker compose -f lab/docker-compose.yml down
```

Then check `tests/test_examples.py` still passes.
