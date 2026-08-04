# Seeding the subordinates from the master (one-off fill)

For the case where the data already exists **only on the master**, and
each site's new phpIPAM instance needs to be filled with its own section
before the normal one-way sync is switched on.

```
   ONE-OFF FILL (this document)              THEN, FOREVER AFTER (DEPLOYMENT.md)

   MASTER                                    site-a ──┐
     ├─ Site-A ──▶ site-a phpIPAM            site-b ──┤
     ├─ Site-B ──▶ site-b phpIPAM            site-c ──┼──▶ MASTER
     ├─ Site-C ──▶ site-c phpIPAM            site-d ──┤
     ├─ Site-D ──▶ site-d phpIPAM            site-e ──┘
     └─ Site-E ──▶ site-e phpIPAM
```

The same two scripts do it. `ipam_export.py` reads whatever is in
`source:` and `ipam_import.py` writes whatever is in `target:` — neither
is tied to a particular instance, so pointing them the other way round is
a configuration change, not a different mode. **Verified end to end**:
filling an empty instance from a master and then running the real forward
sync reports `changes: none`, with folders, nesting, VLANs, VRFs, custom
fields on both subnets and addresses, and IPv6 all intact and re-keyed to
the new instance's own ids.

**Run it once per section.** Five sections, five instances, five fills.
Each is independent; do them one at a time and verify each before moving
on.

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

**The fill configuration must not survive the fill.** During it, the
subordinate is a *write target* and its API app is read/write. Once the
real sync starts, the subordinate is the source of truth and the master
is the write target. If a fill config were still on disk and something
ran it — a stale cron entry, a shell history recall, a colleague — the
master would overwrite the site's own instance with a copy of itself.

So: the fill configs live somewhere temporary, they are deleted when
done, and the subordinate's API app goes back to **Read** the moment its
fill is verified. Both are in [Step 4](#step-4--clean-up-then-reverse-the-direction),
and neither is optional.

Also: leave `delete_drift` **off** for the whole fill. The target is
empty, so there is nothing to reconcile away, and a mis-scoped export
should fail to add rather than succeed at removing.

---

## Step 0 — audit VLANs and VRFs per section

This is the step that makes the fill per-section, and it is the one that
is easy to skip and expensive to undo.

**Subnets, addresses and folders are already per-section** — they live in
a section and are exported with it. VLANs and VRFs are not:

| Object | How it is scoped to a section | Per-section by default? |
|---|---|---|
| Subnet, address, folder | lives in the section | yes |
| VRF | its own `sections` list | yes |
| VLAN | via its **L2 domain**'s section list | **only if the domain is** |
| VLAN in the `default` domain | domain 1 is in **every** section, implicitly | **no** |

phpIPAM puts the `default` L2 domain (id 1) in every section, so **every
VLAN in it is exported with every section**. Fill five instances from a
master whose VLANs sit in `default`, and all five instances get all five
sites' VLANs.

Nothing downstream corrects this. Once site A's instance holds site B's
VLANs, the normal sync will faithfully carry them back up, and they will
never be deleted — VLANs in the `default` domain are deliberately exempt
from strict mirror, because on a fan-in master they cannot be attributed
to one site (see DEPLOYMENT.md Part 5).

**So, before filling anything:** give each site an L2 domain of its own
on the master, scoped to that site's section, and move that site's VLANs
into it. Then check what is left behind:

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

An empty list is what you want. A VLAN that genuinely is shared by every
site can stay there — just know it will exist on all five instances, by
design.

**VRFs need a decision rather than a fix.** A VRF's `sections` list is
honoured exactly: a VRF listing only `Site-A` goes to site A alone, and
one listing `Site-A;Site-B` is exported with *both* fills and will exist
on both instances. That is usually right — it genuinely does serve both —
but check the list matches your intent before filling, because after the
fill each instance owns its own copy:

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

Per instance, before its fill. The importer writes values, never schema,
so anything schema-shaped has to exist first.

- **The section**, created by hand, with the permissions you want. Note
  its name — it is the right-hand side of `section_map` below, and it
  does not have to match the master's name for that site.
- **The custom fields**, same names as the master, on **both**
  `subnets` and `ipaddresses` as applicable. Administration → Custom
  fields. A record carrying a field the instance does not have is
  rejected outright with `Invalid request key <name>`.
- **"Nest custom fields" = Yes** on this instance's API app. It is
  needed later anyway, when this instance becomes the source — without
  it the exporter cannot tell a custom field from a phpIPAM field it
  does not know.
- **Strict mode OFF** on the section (Administration → Sections). The
  fill creates folders and nested subnets, both of which phpIPAM's
  overlap check can refuse.
- **API app permissions: Read/Write, temporarily.** The subordinate's app
  is meant to be Read so the exporter physically cannot write; the fill
  needs write. Step 4 puts it back.

The master needs nothing beyond an app with **Read** — the one it already
has for the sync is fine.

---

## Step 2 — the fill, per section

No git is involved. A snapshot is a directory; for a one-off, carry it
across however you normally move files across the air gap.

Two configs, both temporary. Using site A as the example, where the
master calls the section `Site-A` and the instance calls it `Networks`:

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

Note `sections:` is the same on both sides — it names what is *in the
snapshot*, which was written by the master. `section_map` is what
translates it. This is the mirror image of the normal direction, where
the subordinate exports `Networks` and the master maps it to `Site-A`.

```bash
# on the master
./ipam_export.py --config /tmp/fill/site-a-export.yml --out-dir /tmp/fill/site-a

#   ... carry /tmp/fill/site-a across ...

# on the site instance -- dry run first, it writes nothing
./ipam_import.py --config /tmp/fill/site-a-import.yml --snapshot-dir /tmp/fill/site-a
./ipam_import.py --config /tmp/fill/site-a-import.yml --snapshot-dir /tmp/fill/site-a --apply
```

Read the dry run before applying it. Everything should be a `create_*`;
this instance is empty, so an `update_*` or a `drift_*` means the section
is not as empty as you think.

---

## Step 3 — verify before moving on

Configure the **real, forward** direction for this site — the one that
will run forever — and run it as a dry run:

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

**That is the acceptance test for the fill**, and it is worth more than
counting records by hand: it proves every object round-tripped through
the snapshot format and back, resolved to the same natural keys, and that
the two instances now agree despite sharing no ids.

Anything it wants to change is something that did not survive the trip.
The usual causes, in order of likelihood:

| It wants to | Because |
|---|---|
| `update_subnet`/`update_address` on the same custom field every run | "Nest custom fields" differs between the two API apps |
| `create_*` for records you can see on both | the section names or `section_map` do not line up |
| `link_subnet` for a VLAN | the VLAN landed in a different L2 domain than the master has it in |
| `drift_*` for records only the master has | something was not carried — check `dropped_source_fields` in the master's export manifest |

Fix it, re-run, and only move to the next site once this one is clean.

---

## Step 4 — clean up, then reverse the direction

Per instance, as soon as its verification passes:

1. **Delete the fill configs and the carried snapshot.**
   `rm -rf /tmp/fill/site-a*`. See
   [the risk](#the-one-risk-worth-reading-first).
2. **Set the instance's API app back to Read.** From here on it is a
   source, and the exporter must not be able to write to it. This is what
   makes "the exporter never writes" enforced by phpIPAM rather than
   trusted.
3. **Leave strict mode off** if you want it off for other reasons, but
   the fill no longer needs it. The master's replicated sections do still
   need it off — that is a separate requirement (DEPLOYMENT.md Part 2.1).

Then follow [DEPLOYMENT.md](DEPLOYMENT.md) from Part 1.4 for that site:
its own config, its own mirrored repo, and cron. The data is already in
place, so its first real export/import is the no-op you just verified.

Only once **all five** sites are filled, verified and pushing normally
should you consider strict mirror (DEPLOYMENT.md Part 5). Turning it on
mid-fill would let a half-configured site's snapshot delete on the
master.

---

## Troubleshooting

**`Target has no section named 'Networks'`** — create it on the instance
first, or fix `section_map`. Do not set `create_missing_sections: true`
to get around it: a section created that way has no permissions and is
invisible to non-admins.

**`Invalid request key Owner`** — that custom field does not exist on the
instance. Create it, same name, on the right table. Note it is per-table:
adding it to `subnets` does nothing for `ipaddresses`.

**`409 ... overlaps with ...`** — strict mode is still on for the
section. Turn it off (Step 1).

**`401 Unauthorized` on the import** — the instance's API app is still
Read. That is the correct resting state; grant Read/Write for the fill
and revoke it after.

**VLANs from another site appeared** — they were in the master's
`default` L2 domain. See [Step 0](#step-0--audit-vlans-and-vrfs-per-section).
Delete them on the instance by hand; nothing will do it automatically,
because default-domain VLANs are exempt from strict mirror by design.

**The verification wants to delete things on the master** — stop. That
means `delete_drift` is on somewhere it should not be during a fill, or
`--delete` was passed. Nothing in this procedure ever deletes.

---

## What this does not do

- **It is not a two-way sync, and must never become one.** The fill is a
  single move in the opposite direction, done once, with the
  configuration destroyed afterwards. There is no support for the two
  instances both being writable, and nothing in this tool detects it.
- **It does not carry what the tool never carries** — devices, locations,
  nameservers, permissions, customers, users, or the changelog. See
  "What is and isn't replicated" in [README.md](README.md).
- **It does not create schema.** Custom fields must exist first; this is
  the single most common cause of a failed fill.
- **It does not preserve ids**, deliberately. The new instance allocates
  its own, and every relationship is rebuilt from natural keys. That is
  the property that makes the fill safe to run into an instance that is
  not empty — though it should be.
