# Seeding the subordinates from the master (one-off fill)

For when the data exists **only on the master** and each site's new
phpIPAM instance must be filled with its own section before the normal
one-way sync starts.

```
   ONE-OFF FILL (this document)              THEN, FOREVER AFTER (DEPLOYMENT.md)

   MASTER                                    site-a ──┐
     ├─ Site-A ──▶ site-a phpIPAM            site-b ──┤
     ├─ Site-B ──▶ site-b phpIPAM            site-c ──┼──▶ MASTER
     ├─ Site-C ──▶ site-c phpIPAM            site-d ──┤
     ├─ Site-D ──▶ site-d phpIPAM            site-e ──┘
     └─ Site-E ──▶ site-e phpIPAM
```

Same two scripts, pointed the other way: `ipam_export.py` reads whatever
is in `source:`, `ipam_import.py` writes whatever is in `target:`. A
filled instance followed by the real forward sync reports
`changes: none`, with folders, nesting, VLANs, VRFs, custom fields on
subnets and addresses, and IPv6 intact and re-keyed to the new
instance's ids.

**Run it once per section**, one at a time, verifying each before the
next.

**Contents**

1. [The one risk worth reading first](#the-one-risk-worth-reading-first)
2. [Step 0 — audit VLANs and VRFs per section](#step-0--audit-vlans-and-vrfs-per-section)
3. [Step 1 — prepare each target instance](#step-1--prepare-each-target-instance)
4. [Step 2 — the fill, per section](#step-2--the-fill-per-section)
5. [Step 3 — verify before moving on](#step-3--verify-before-moving-on)
6. [Step 4 — clean up, then reverse the direction](#step-4--clean-up-then-reverse-the-direction)
7. [Troubleshooting](#troubleshooting)
8. [What this does not do](#what-this-does-not-do)

---

## The one risk worth reading first

**The fill configuration must not survive the fill.** During it the
subordinate is a write target with a read/write API app. Afterwards the
subordinate is the source of truth. A leftover fill config run by
accident (stale cron, shell history) would overwrite the site's instance
with the master's copy.

Keep the fill configs somewhere temporary, delete them when done, and
set the subordinate's API app back to **Read** once its fill is
verified ([Step 4](#step-4--clean-up-then-reverse-the-direction)). Both
are mandatory.

Leave `delete_drift` **off** for the whole fill.

---

## Step 0 — audit VLANs and VRFs per section

Subnets, addresses and folders live in a section and are exported with
it. VLANs and VRFs are not:

| Object | How it is scoped to a section | Per-section by default? |
|---|---|---|
| Subnet, address, folder | lives in the section | yes |
| VRF | its own `sections` list | yes |
| VLAN | via its **L2 domain**'s section list | **only if the domain is** |
| VLAN in the `default` domain | domain 1 is in **every** section, implicitly | **no** |

**Every VLAN in the `default` L2 domain (id 1) is exported with every
section.** Fill five instances from a master whose VLANs sit in
`default`, and all five get all five sites' VLANs. The normal sync then
carries them back up and never deletes them (default-domain VLANs are
exempt from strict mirror; DEPLOYMENT.md Part 5).

**Before filling:** give each site its own L2 domain on the master,
scoped to that site's section, and move its VLANs into it. Then check
what is left:

```bash
# On the master. Anything listed here will be copied to EVERY instance.
curl -s -H "token: $PHPIPAM_MASTER_TOKEN" -H 'Content-Type: application/json' \
     https://ipam-master/api/sync/vlans/ |
  python3 -c '
import sys, json
rows = json.load(sys.stdin).get("data") or []
shared = [v for v in rows if str(v.get("domainId")) == "1"]
print("%d VLAN(s) in the default domain:" % len(shared))
for v in shared:
    print("   %6s  %s" % (v.get("number"), v.get("name")))'
```

The list should be empty, apart from VLANs that genuinely belong on
every instance.

**VRFs:** a VRF's `sections` list is honoured exactly. One listing
`Site-A;Site-B` is exported with both fills and exists on both
instances afterwards. Check the lists match your intent:

```bash
curl -s -H "token: $PHPIPAM_MASTER_TOKEN" -H 'Content-Type: application/json' \
     https://ipam-master/api/sync/vrfs/ |
  python3 -c '
import sys, json
for v in json.load(sys.stdin).get("data") or []:
    print("%-20s sections=%s" % (v.get("name"), v.get("sections")))'
```

---

## Step 1 — prepare each target instance

The importer writes values, never schema. Per instance, before its fill:

- **The section**, created by hand with the permissions you want. Its
  name is the right-hand side of `section_map` below; it need not match
  the master's.
- **The custom fields**, same names as the master, on `subnets` and
  `ipaddresses` as applicable (Administration → Custom fields). A record
  carrying a missing field is rejected with `Invalid request key <name>`.
- **"Nest custom fields" = Yes** on this instance's API app (needed when
  it becomes the source).
- **Strict mode OFF** on the section (Administration → Sections); the
  fill creates folders and nested subnets the overlap check can refuse.
- **API app permissions: Read/Write, temporarily.** Step 4 puts it back
  to Read.

The master needs only an app with **Read**; the sync's app will do.

---

## Step 2 — the fill, per section

No git is involved. A snapshot is a directory; carry it across the air
gap however you normally move files.

Two temporary configs. Site A, where the master calls the section
`Site-A` and the instance calls it `Networks`:

```yaml
# /tmp/fill/site-a-export.yml   -- read FROM the master
source:
  base_url: https://ipam-master.example.com
  app_id: sync
  token_env: PHPIPAM_MASTER_TOKEN
  verify_ssl: true
sections:
  - Site-A                      # the section name ON THE MASTER
```

```yaml
# /tmp/fill/site-a-import.yml   -- write INTO the new instance
target:
  base_url: https://ipam.site-a.internal
  app_id: sync
  token_env: PHPIPAM_SITE_A_TOKEN
  verify_ssl: true
sections:
  - Site-A                      # still the master's name: it is what the snapshot says
section_map:
  Site-A: Networks              # where it lands on this instance
options:
  delete_drift: false           # never true during a fill
```

`sections:` is the same on both sides: it names what is *in the
snapshot*. `section_map` translates it.

```bash
# on the master
./ipam_export.py --config /tmp/fill/site-a-export.yml --out-dir /tmp/fill/site-a

#   ... carry /tmp/fill/site-a across ...

# on the site instance -- dry run first, it writes nothing
./ipam_import.py --config /tmp/fill/site-a-import.yml --snapshot-dir /tmp/fill/site-a
./ipam_import.py --config /tmp/fill/site-a-import.yml --snapshot-dir /tmp/fill/site-a --apply
```

The dry run should be all `create_*`. An `update_*` or `drift_*` means
the section is not empty.

---

## Step 3 — verify before moving on

Configure the **real, forward** direction for this site and dry-run it:

```bash
# on the site instance
./ipam_export.py --config /opt/phpipam-sync/config.yml --out-dir /srv/ipam-data

# on the master, against that site only
./ipam_import.py --config /opt/phpipam-sync/config.master.yml --source site-a --pull
```

It must say:

```
changes: none -- target already matches the snapshot
```

That is the acceptance test. Anything it wants to change did not survive
the trip:

| It wants to | Because |
|---|---|
| `update_subnet`/`update_address` on the same custom field every run | "Nest custom fields" differs between the two API apps |
| `create_*` for records you can see on both | the section names or `section_map` do not line up |
| `link_subnet` for a VLAN | the VLAN landed in a different L2 domain than the master has it in |
| `drift_*` for records only the master has | something was not carried — check `dropped_source_fields` in the master's export manifest |

Fix, re-run, and move on only once this site is clean.

---

## Step 4 — clean up, then reverse the direction

Per instance, once verification passes:

1. **Delete the fill configs and the carried snapshot:**
   `rm -rf /tmp/fill/site-a*`.
2. **Set the instance's API app back to Read.**
3. **Strict mode** may stay off; the fill no longer needs it. The
   master's replicated sections still need it off (DEPLOYMENT.md Part
   2.1).

Then follow [DEPLOYMENT.md](DEPLOYMENT.md) from Part 1.4 for that site.
Its first real export/import is the no-op you just verified.

Consider strict mirror (DEPLOYMENT.md Part 5) only once **all** sites
are filled, verified and pushing normally; mid-fill, a half-configured
site's snapshot could delete on the master.

---

## Troubleshooting

**`Target has no section named 'Networks'`**: create it on the instance,
or fix `section_map`. Do not set `create_missing_sections: true`: a
section created that way has no permissions and is invisible to
non-admins.

**`Invalid request key Owner`**: the custom field is missing on the
instance. Create it, same name, on the right table; `subnets` and
`ipaddresses` are separate.

**`409 ... overlaps with ...`**: strict mode is still on (Step 1).

**`401 Unauthorized` on the import**: the instance's API app is still
Read. Grant Read/Write for the fill, revoke after.

**VLANs from another site appeared**: they were in the master's
`default` L2 domain ([Step 0](#step-0--audit-vlans-and-vrfs-per-section)).
Delete them on the instance by hand; nothing will do it automatically.

**The verification wants to delete things on the master**: stop.
`delete_drift` is on somewhere, or `--delete` was passed. Nothing in
this procedure deletes.

---

## What this does not do

- **Not a two-way sync.** Both instances writable is unsupported and
  undetected.
- **Does not carry what the tool never carries**: devices, locations,
  nameservers, permissions, customers, users, the changelog. See
  "What is and isn't replicated" in [README.md](README.md).
- **Does not create schema.** Missing custom fields are the most common
  cause of a failed fill.
- **Does not preserve ids.** The new instance allocates its own and
  relationships are rebuilt from natural keys.
