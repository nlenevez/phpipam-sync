#!/usr/bin/env bash
#
# End-to-end verification of phpipam-sync against the two real phpIPAM
# instances brought up by setup.sh.
#
#   export PHPIPAM_SRC_TOKEN=... PHPIPAM_DST_TOKEN=...
#   ./verify.sh                 # re-seeds the lab, then verifies
#   ./verify.sh --no-setup      # verify against the lab as it stands
#
# It re-seeds by default because step 7 deliberately mutates BOTH
# instances (renaming a subnet, deleting an address upstream, adding a
# replica-only record) to test change propagation and drift. Those
# mutations are the point, but they mean a second run against the same
# lab would start from a state the earlier checks do not expect. Seeding
# first makes the whole thing one reproducible command rather than a
# sequence you have to remember to run in order.
#
# Every check asserts rather than just printing, so this either says
# ALL CHECKS PASSED or exits non-zero naming what failed. The properties
# checked are the ones the design actually promises:
#
#   1. the exporter never writes (its API app is read-only)
#   2. only the configured section is replicated
#   3. records land on the replica with the replica's OWN ids
#   4. subnet nesting is rebuilt from the parent CIDR, not the parent id
#   5. a dry run writes nothing
#   6. a steady-state re-run writes nothing (cron-safe)
#   7. changes propagate as minimal field-level updates
#   8. deletions upstream and local records on the replica both survive
#   9. custom fields and IPv6 replicate
#  9b. a subnet re-parented upstream is re-parented on the replica,
#      including the case where the new parent is created in the same run
#  10. --delete makes it a strict mirror, and the safety limit stops a
#      snapshot that would wipe the replica

set -uo pipefail
cd "$(dirname "$0")"
LAB=$(pwd)
TOOL=$(cd .. && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

: "${PHPIPAM_SRC_TOKEN:?set PHPIPAM_SRC_TOKEN (see setup.sh output)}"
: "${PHPIPAM_DST_TOKEN:?set PHPIPAM_DST_TOKEN (see setup.sh output)}"

if [ "${1:-}" != "--no-setup" ]; then
    printf '\n\033[1m== re-seeding the lab (pass --no-setup to skip)\033[0m\n'
    ./setup.sh >/dev/null 2>&1 || { echo "setup.sh failed; run it directly to see why"; exit 1; }
fi

FAILURES=0
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILURES=$((FAILURES+1)); }
check(){ # check <description> <expected> <actual>
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}
sql1() { docker compose exec -T db1 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }
sql2() { docker compose exec -T db2 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }

export PYTHONWARNINGS=ignore   # silence self-signed-cert warnings

say "building the one-way mirror and two host checkouts"
git init -q --bare "$WORK/mirror.git"
git clone -q "$WORK/mirror.git" "$WORK/side1-data"
git clone -q "$WORK/mirror.git" "$WORK/side2-data"
git -C "$WORK/side1-data" config user.email lab@lab
git -C "$WORK/side1-data" config user.name lab

cat > "$WORK/config1.yml" <<EOF
source:
  base_url: https://127.0.0.1:8443
  app_id: sync
  token_env: PHPIPAM_SRC_TOKEN
  verify_ssl: false
sections:
  - Shared
EOF
cat > "$WORK/config2.yml" <<EOF
target:
  base_url: https://127.0.0.1:8444
  app_id: sync
  token_env: PHPIPAM_DST_TOKEN
  verify_ssl: false
sections:
  - Shared
EOF

export_now() { python3 "$TOOL/ipam_export.py" --config "$WORK/config1.yml" --out-dir "$WORK/side1-data" "$@"; }
import_now() { python3 "$TOOL/ipam_import.py" --config "$WORK/config2.yml" --snapshot-dir "$WORK/side2-data" "$@"; }
ship() {
    git -C "$WORK/side1-data" add -A
    git -C "$WORK/side1-data" commit -q -m "snapshot" >/dev/null 2>&1
    git -C "$WORK/side1-data" push -q origin HEAD:main
    git -C "$WORK/side2-data" pull -q --ff-only origin main
}

say "1. the source API app rejects writes"
RC=$(curl -sk -H "Content-Type: application/json" -H "token: $PHPIPAM_SRC_TOKEN" \
     -X POST https://127.0.0.1:8443/api/sync/subnets/ \
     -d '{"subnet":"10.77.0.0","mask":"24","sectionId":"1"}' \
     | python3 -c 'import sys,json;print(json.load(sys.stdin)["code"])')
check "source app is read-only (write returns 401)" "401" "$RC"

say "2. export from instance 1"
export_now > "$WORK/export.log" 2>&1 || { cat "$WORK/export.log"; fail "export failed"; }
check "4 subnets exported"  "4" "$(python3 -c 'import json;print(json.load(open("'"$WORK"'/side1-data/manifest.json"))["subnet_count"])')"
check "5 addresses exported" "5" "$(python3 -c 'import json;print(json.load(open("'"$WORK"'/side1-data/manifest.json"))["address_count"])')"
if grep -rq "192.168.99\|local-host" "$WORK/side1-data" 2>/dev/null; then
    fail "the out-of-scope 'LocalOnly' section leaked into the snapshot"
else
    pass "out-of-scope section is not in the snapshot"
fi
if grep -q "dropped_source_fields.*: {}" <(python3 -c 'import json;print(json.dumps(json.load(open("'"$WORK"'/side1-data/manifest.json"))))') ; then
    pass "no unrecognised source fields (field policy covers this phpIPAM)"
else
    printf '  \033[33mNOTE\033[0m unrecognised source fields: %s\n' \
      "$(python3 -c 'import json;print(json.load(open("'"$WORK"'/side1-data/manifest.json"))["dropped_source_fields"])')"
fi

ship

say "3. dry run must write nothing"
BEFORE=$(sql2 "SELECT COUNT(*) FROM subnets;")
import_now > "$WORK/dry.log" 2>&1
check "replica untouched by dry run" "$BEFORE" "$(sql2 'SELECT COUNT(*) FROM subnets;')"
grep -q "dry run -- nothing was written" "$WORK/dry.log" \
    && pass "dry run says so explicitly" || fail "dry run banner missing"

say "4. apply"
import_now --apply > "$WORK/apply.log" 2>&1
RC=$?
check "apply exit status" "0" "$RC"
grep -q "applied 20 change(s), 0 error(s)" "$WORK/apply.log" \
    && pass "20 changes applied, no errors" \
    || fail "unexpected apply result: $(grep -E '^applied' "$WORK/apply.log")"

say "5. the replica matches, with its own ids"
# isFolder=0: folders live in the same table and are counted separately
# at step 11c.
check "replica subnet count"  "4" \
      "$(sql2 'SELECT COUNT(*) FROM subnets WHERE isFolder=0;')"
check "replica address count" "5" "$(sql2 'SELECT COUNT(*) FROM ipaddresses;')"

SRC_ID=$(sql1 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.20.5.0') AND mask=24;")
DST_ID=$(sql2 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.20.5.0') AND mask=24;")
if [ -n "$SRC_ID" ] && [ -n "$DST_ID" ] && [ "$SRC_ID" != "$DST_ID" ]; then
    pass "same subnet has different ids on each instance ($SRC_ID vs $DST_ID)"
else
    fail "ids did not diverge (src=$SRC_ID dst=$DST_ID) -- test is not proving anything"
fi

# The heart of it: the child's masterSubnetId must be the REPLICA's id
# for the parent, never the source's.
DST_PARENT=$(sql2 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.20.0.0') AND mask=16;")
DST_MASTER=$(sql2 "SELECT masterSubnetId FROM subnets WHERE id=$DST_ID;")
SRC_MASTER=$(sql1 "SELECT masterSubnetId FROM subnets WHERE id=$SRC_ID;")
check "nesting rebuilt against the replica's own parent id" "$DST_PARENT" "$DST_MASTER"
if [ "$DST_MASTER" = "$SRC_MASTER" ]; then
    fail "replica's masterSubnetId equals the source's -- an id was copied across"
else
    pass "replica's masterSubnetId differs from the source's ($DST_MASTER vs $SRC_MASTER)"
fi

# gatewayId is excluded from the snapshot; the replica must recompute it
# from the is_gateway flag on the address.
DST_GW=$(curl -sk -H "token: $PHPIPAM_DST_TOKEN" "https://127.0.0.1:8444/api/sync/subnets/$DST_ID/" \
         | python3 -c 'import sys,json;d=json.load(sys.stdin)["data"];print((d.get("gateway") or {}).get("ip_addr",""))')
check "replica recomputed its own gateway" "10.20.5.2" "$DST_GW"

say "6. steady state re-run is a no-op (cron-safe)"
import_now --apply > "$WORK/again.log" 2>&1
grep -q "changes: none" "$WORK/again.log" \
    && pass "second apply makes no changes" \
    || fail "second apply was not idempotent: $(grep -E '^summary' "$WORK/again.log")"

say "7. changes propagate as minimal updates"
sql1 "UPDATE subnets SET description='renamed upstream' WHERE id=$SRC_ID;" >/dev/null
sql1 "UPDATE ipaddresses SET hostname='sw10-replaced' WHERE ip_addr=INET_ATON('10.20.5.10');" >/dev/null
sql1 "INSERT INTO ipaddresses (subnetId,ip_addr,hostname,description,is_gateway,owner,state)
      VALUES ($SRC_ID,INET_ATON('10.20.5.30'),'new-server','added upstream',0,'netops',2);" >/dev/null
sql1 "DELETE FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.20');" >/dev/null
# A record that exists only on the replica; it must survive every sync.
sql2 "INSERT INTO ipaddresses (subnetId,ip_addr,hostname,description,is_gateway,owner,state)
      VALUES ($DST_ID,INET_ATON('10.20.5.99'),'replica-local','replica only',0,'local',2);" >/dev/null

export_now >/dev/null 2>&1
ship
import_now --apply > "$WORK/update.log" 2>&1
grep -q "applied 3 change(s), 0 error(s)" "$WORK/update.log" \
    && pass "exactly 3 changes applied (1 subnet, 1 address update, 1 create)" \
    || fail "unexpected update result: $(grep -E '^applied' "$WORK/update.log")"
check "subnet description propagated" "renamed upstream" \
      "$(sql2 "SELECT description FROM subnets WHERE id=$DST_ID;")"
check "address hostname propagated" "sw10-replaced" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.10');")"
check "new address created" "new-server" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.30');")"

say "8. additive: nothing is ever deleted on the replica"
check "record deleted upstream still present on replica" "printer" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.20');")"
check "replica-only record untouched" "replica-local" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.99');")"
grep -q "drift_address .*10.20.5.20" "$WORK/update.log" \
    && pass "upstream deletion reported as drift" || fail "drift not reported for 10.20.5.20"
grep -q "drift_address .*10.20.5.99" "$WORK/update.log" \
    && pass "replica-only record reported as drift" || fail "drift not reported for 10.20.5.99"

say "9. custom fields replicate, whatever they are named"
# phpIPAM custom fields are plain extra columns whose names the admin
# chooses -- 'Owner' has no prefix of any kind, which is what the UI
# actually produces. They are only distinguishable from phpIPAM's own
# fields when the API app has 'nest custom fields' enabled.
check "unprefixed custom field on a subnet"  "netops-team" \
      "$(sql2 "SELECT \`Owner\` FROM subnets WHERE id=$DST_ID;")"
check "prefixed custom field on a subnet"    "prefixed note" \
      "$(sql2 "SELECT custom_Notes FROM subnets WHERE id=$DST_ID;")"
check "unprefixed custom field on an address" "ASSET-0042" \
      "$(sql2 "SELECT AssetTag FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.2');")"

say "10. IPv6 subnets and addresses replicate"
V6=$(sql2 "SELECT id FROM subnets WHERE mask=64;")
if [ -n "$V6" ]; then
    pass "IPv6 /64 present on the replica (id $V6)"
else
    fail "IPv6 /64 was not replicated"
fi
check "IPv6 subnet description" "Shared v6 /64" \
      "$(sql2 "SELECT description FROM subnets WHERE mask=64;")"
check "IPv6 address replicated" "v6-gw" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE subnetId=$V6;")"

say "11. a subnet moved under a different parent moves on the replica"
# masterSubnetId is a local id and is excluded from the replicated field
# set, so a re-parent is invisible to the field comparison and has to be
# detected by comparing parents as CIDRs. Before that was handled, a
# subnet kept its original parent on the replica forever while every
# field still matched -- a structural divergence that looked like a
# clean sync.
#
# The realistic shape of this: someone inserts an intermediate aggregate
# upstream and moves an existing /24 underneath it.
SRC_SEC=$(sql1 "SELECT sectionId FROM subnets WHERE id=$SRC_ID;")
SRC_SUPER=$(sql1 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.20.0.0') AND mask=16;")
sql1 "INSERT INTO subnets (subnet,mask,sectionId,masterSubnetId,description)
      VALUES (INET_ATON('10.20.4.0'),22,$SRC_SEC,$SRC_SUPER,'intermediate aggregate');" >/dev/null
SRC_MID=$(sql1 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.20.4.0') AND mask=22;")
sql1 "UPDATE subnets SET masterSubnetId=$SRC_MID WHERE id=$SRC_ID;" >/dev/null

export_now >/dev/null 2>&1
ship
import_now --apply > "$WORK/reparent.log" 2>&1
grep -q "reparent_subnet .*10.20.5.0/24" "$WORK/reparent.log" \
    && pass "re-parent planned as its own action" \
    || fail "no reparent_subnet action: $(grep -E '^(applied|summary)' "$WORK/reparent.log")"

DST_MID=$(sql2 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.20.4.0') AND mask=22;")
check "replica created the intermediate aggregate" "1" \
      "$([ -n "$DST_MID" ] && echo 1 || echo 0)"
check "replica moved the /24 under it" "$DST_MID" \
      "$(sql2 "SELECT masterSubnetId FROM subnets WHERE id=$DST_ID;")"
# The ids must differ across instances, or this proves nothing about
# natural-key resolution.
[ "$SRC_MID" != "$DST_MID" ] \
    && pass "and did so by CIDR, not by copying the source id ($SRC_MID vs $DST_MID)" \
    || fail "source and replica ids coincide ($SRC_MID) -- test cannot distinguish"

# Re-running must not move it again: comparing a local id against a
# natural key is exactly how the endless-update-loop bug happened.
import_now --apply > "$WORK/reparent-again.log" 2>&1
grep -q "changes: none" "$WORK/reparent-again.log" \
    && pass "re-parent is idempotent (no churn on the next run)" \
    || fail "re-parent churns: $(grep -E '^applied' "$WORK/reparent-again.log")"

say "11b. VLANs and VRFs replicate, including unattached ones"
# Replicated as records in their own right: a VLAN or VRF defined
# upstream but attached to nothing is still part of what the source
# owns. Matched by natural key -- a VLAN by (L2 domain, number), a VRF
# by (section, name) -- because phpIPAM has no unique index on either
# and the ids differ between instances.
check "L2 domain replicated by name" "1" \
      "$(sql2 "SELECT COUNT(*) FROM vlanDomains WHERE name='Site-A';")"
DST_DOM=$(sql2 "SELECT id FROM vlanDomains WHERE name='Site-A';")
SRC_DOM=$(sql1 "SELECT id FROM vlanDomains WHERE name='Site-A';")
[ -n "$DST_DOM" ] && [ "$SRC_DOM" != "$DST_DOM" ] \
    && pass "and with the replica's own domain id ($SRC_DOM vs $DST_DOM)" \
    || fail "domain ids did not diverge (src=$SRC_DOM dst=$DST_DOM)"

# vlanDomains.permissions is a ";"-separated list of SECTION ids despite
# the name -- the importer must scope the domain to the target section.
check "domain scoped to the replica's own section id" "500" \
      "$(sql2 "SELECT permissions FROM vlanDomains WHERE name='Site-A';")"

check "attached VLAN replicated"   "shared-vlan" \
      "$(sql2 "SELECT name FROM vlans WHERE number=100;")"
check "UNattached VLAN replicated" "voice" \
      "$(sql2 "SELECT name FROM vlans WHERE number=200;")"
check "single-digit VLAN replicated" "mgmt" \
      "$(sql2 "SELECT name FROM vlans WHERE number=9;")"
check "attached VRF replicated"    "65000:1" \
      "$(sql2 "SELECT rd FROM vrf WHERE name='CUST-A';")"
check "UNattached VRF replicated"  "65000:2" \
      "$(sql2 "SELECT rd FROM vrf WHERE name='CUST-B';")"
check "VRF scoped to the replica's own section id" "500" \
      "$(sql2 "SELECT sections FROM vrf WHERE name='CUST-A';")"

# The subnet must point at the REPLICA's vlan id, never the source's.
DST_VLAN=$(sql2 "SELECT vlanId FROM subnets WHERE id=$DST_ID;")
DST_VLAN_NUM=$(sql2 "SELECT number FROM vlans WHERE vlanId=$DST_VLAN;")
check "subnet linked to the right VLAN by number" "100" "$DST_VLAN_NUM"
SRC_VLAN=$(sql1 "SELECT vlanId FROM subnets WHERE id=$SRC_ID;")
[ "$SRC_VLAN" != "$DST_VLAN" ] \
    && pass "and by natural key, not by copying the id ($SRC_VLAN vs $DST_VLAN)" \
    || fail "vlanId was copied across instances ($SRC_VLAN)"

DST_V4=$(sql2 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.30.0.0') AND mask=24;")
DST_VRF=$(sql2 "SELECT vrfId FROM subnets WHERE id=$DST_V4;")
check "subnet linked to the right VRF" "CUST-A" \
      "$(sql2 "SELECT name FROM vrf WHERE vrfId=$DST_VRF;")"

# Moving a subnet to a different VLAN upstream must move it here too --
# vlanId is a local id, so this is the same class of bug as re-parenting.
sql1 "UPDATE subnets SET vlanId=11 WHERE id=$SRC_ID;" >/dev/null
export_now >/dev/null 2>&1
ship
import_now --apply > "$WORK/vlanmove.log" 2>&1
grep -q "link_subnet" "$WORK/vlanmove.log" \
    && pass "VLAN change planned as its own action" \
    || fail "no link_subnet action: $(grep -E '^applied' "$WORK/vlanmove.log")"
DST_VLAN2=$(sql2 "SELECT vlanId FROM subnets WHERE id=$DST_ID;")
check "subnet moved to the new VLAN" "200" \
      "$(sql2 "SELECT number FROM vlans WHERE vlanId=$DST_VLAN2;")"
import_now --apply > "$WORK/vlanmove2.log" 2>&1
grep -q "changes: none" "$WORK/vlanmove2.log" \
    && pass "VLAN link is idempotent (no churn on the next run)" \
    || fail "VLAN link churns: $(grep -E '^applied' "$WORK/vlanmove2.log")"

say "11c. folders replicate, and subnets land inside them"
# A phpIPAM folder is a subnets row with isFolder=1 and no network, its
# name held in `description`. The exporter used to skip those rows
# outright, so a subnet inside one arrived at the top of its section --
# right data, silently wrong structure.
check "top-level folder replicated" "1" \
      "$(sql2 "SELECT COUNT(*) FROM subnets WHERE isFolder=1 AND description='Datacentre';")"
check "nested folder replicated"    "1" \
      "$(sql2 "SELECT COUNT(*) FROM subnets WHERE isFolder=1 AND description='Rack A';")"
check "EMPTY folder replicated"     "1" \
      "$(sql2 "SELECT COUNT(*) FROM subnets WHERE isFolder=1 AND description='Spare';")"

DST_DC=$(sql2 "SELECT id FROM subnets WHERE isFolder=1 AND description='Datacentre';")
DST_RACK=$(sql2 "SELECT id FROM subnets WHERE isFolder=1 AND description='Rack A';")
check "folder nesting rebuilt by path" "$DST_DC" \
      "$(sql2 "SELECT masterSubnetId FROM subnets WHERE id=$DST_RACK;")"
SRC_RACK=$(sql1 "SELECT id FROM subnets WHERE isFolder=1 AND description='Rack A';")
[ -n "$DST_RACK" ] && [ "$SRC_RACK" != "$DST_RACK" ] \
    && pass "and with the replica's own folder id ($SRC_RACK vs $DST_RACK)" \
    || fail "folder ids did not diverge (src=$SRC_RACK dst=$DST_RACK)"

DST_INFOLDER=$(sql2 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.30.0.0') AND mask=24;")
check "subnet placed inside the folder" "$DST_RACK" \
      "$(sql2 "SELECT masterSubnetId FROM subnets WHERE id=$DST_INFOLDER;")"

# Folders must not be mistaken for subnets in either direction. Compared
# against the source's own non-folder count rather than a fixed number,
# because step 11 legitimately adds a subnet upstream before this runs.
check "folders not counted as subnets in the snapshot" \
      "$(sql1 'SELECT COUNT(*) FROM subnets WHERE sectionId=1 AND isFolder=0;')" \
      "$(python3 -c 'import json;print(json.load(open("'"$WORK"'/side1-data/manifest.json"))["subnet_count"])')"

# Moving a subnet out of a folder must propagate, like any re-parent.
sql1 "UPDATE subnets SET masterSubnetId=0 WHERE id=13;" >/dev/null
export_now >/dev/null 2>&1
ship
import_now --apply > "$WORK/folder-move.log" 2>&1
check "subnet moved out of the folder" "0" \
      "$(sql2 "SELECT masterSubnetId FROM subnets WHERE id=$DST_INFOLDER;")"
import_now --apply > "$WORK/folder-move2.log" 2>&1
grep -q "changes: none" "$WORK/folder-move2.log" \
    && pass "folder placement is idempotent (no churn on the next run)" \
    || fail "folder placement churns: $(grep -E '^applied' "$WORK/folder-move2.log")"

say "12. strict mirror: --delete removes what the snapshot dropped"
# Up to here the run has been additive, so the replica still holds
# 10.20.5.20 (deleted upstream in step 7) and 10.20.5.99 (replica-only).
# Both are orphans, so a strict-mirror run must remove exactly those two.
import_now --delete > "$WORK/delete-dry.log" 2>&1
grep -q "DELETIONS (2)" "$WORK/delete-dry.log" \
    && pass "dry run lists both deletions" \
    || fail "expected 2 deletions in the dry run: $(grep -E '^summary' "$WORK/delete-dry.log")"
check "dry run with --delete still deletes nothing" "printer" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.20');")"

import_now --delete --apply > "$WORK/delete.log" 2>&1
check "strict-mirror apply exit status" "0" "$?"
check "record deleted upstream is now gone from the replica" "" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.20');")"
check "replica-only record is now gone too" "" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.99');")"
check "records still in the snapshot survived" "gw-core" \
      "$(sql2 "SELECT hostname FROM ipaddresses WHERE ip_addr=INET_ATON('10.20.5.2');")"

import_now --delete --apply > "$WORK/delete2.log" 2>&1
grep -q "changes: none" "$WORK/delete2.log" \
    && pass "strict mirror is idempotent too" \
    || fail "second strict-mirror run was not a no-op: $(grep -E '^summary' "$WORK/delete2.log")"

say "12b. strict mirror also removes folders, VLANs and VRFs"
# Deletion of these is scoped far more tightly than subnets. A subnet is
# safe to reconcile because each source owns its section outright; an L2
# domain can serve several sections, and domain 1 ("default") serves
# EVERY section implicitly. So only objects scoped to this one section
# are ever candidates -- otherwise one subordinate would delete another's
# VLANs on a fan-in master.

# Records created only on the replica, in both kinds of domain. The one
# in the shared default domain must survive; the one in this section's
# own domain must not.
DST_DOM=$(sql2 "SELECT id FROM vlanDomains WHERE name='Site-A';")
sql2 "INSERT INTO vlans (domainId,name,number,description)
      VALUES (1,'replica-only-shared',4001,'in the DEFAULT domain');" >/dev/null
sql2 "INSERT INTO vlans (domainId,name,number,description)
      VALUES ($DST_DOM,'replica-only-owned',4002,'in this section own domain');" >/dev/null

# Deleted upstream: a VLAN, a VRF and an empty folder.
sql1 "DELETE FROM vlans WHERE number=200;" >/dev/null
sql1 "DELETE FROM vrf WHERE name='CUST-B';" >/dev/null
sql1 "DELETE FROM subnets WHERE isFolder=1 AND description='Spare';" >/dev/null

export_now >/dev/null 2>&1
ship
import_now --delete --apply > "$WORK/l2del.log" 2>&1

check "VLAN deleted upstream is gone from the replica" "0" \
      "$(sql2 "SELECT COUNT(*) FROM vlans WHERE number=200;")"
check "VRF deleted upstream is gone from the replica" "0" \
      "$(sql2 "SELECT COUNT(*) FROM vrf WHERE name='CUST-B';")"
check "empty folder deleted upstream is gone from the replica" "0" \
      "$(sql2 "SELECT COUNT(*) FROM subnets WHERE isFolder=1 AND description='Spare';")"

# The safety rule, asserted against real phpIPAM rather than argued for.
check "replica VLAN in the SHARED default domain survives" "1" \
      "$(sql2 "SELECT COUNT(*) FROM vlans WHERE number=4001;")"
check "replica VLAN in this section's OWN domain is removed" "0" \
      "$(sql2 "SELECT COUNT(*) FROM vlans WHERE number=4002;")"

# Deleting a VLAN must not take the subnets pointing at it: phpIPAM
# nulls their vlanId instead.
check "subnets survive their VLAN being deleted" \
      "$(sql1 'SELECT COUNT(*) FROM subnets WHERE sectionId=1 AND isFolder=0;')" \
      "$(sql2 'SELECT COUNT(*) FROM subnets WHERE isFolder=0;')"

import_now --delete --apply > "$WORK/l2del2.log" 2>&1
grep -q "changes: none" "$WORK/l2del2.log" \
    && pass "l2 deletion is idempotent (no churn on the next run)" \
    || fail "l2 deletion churns: $(grep -E '^applied' "$WORK/l2del2.log")"

say "13. the delete safety limit refuses a mass deletion"
# The limit is a FRACTION of the in-scope records, with a small absolute
# floor so tiny datasets are not permanently blocked. The seed data alone
# is under that floor, so bulk up first -- otherwise this step would pass
# for the wrong reason and prove nothing.
SRC30=$(sql1 "SELECT id FROM subnets WHERE subnet=INET_ATON('10.30.0.0');")
sql1 "INSERT INTO ipaddresses (subnetId,ip_addr,hostname,description,is_gateway,owner,state)
      SELECT $SRC30, INET_ATON('10.30.0.0')+seq, CONCAT('bulk-',seq), 'bulk', 0, 'netops', 2
      FROM (SELECT 10+ROW_NUMBER() OVER () AS seq FROM information_schema.columns LIMIT 60) t;" >/dev/null
export_now >/dev/null 2>&1; ship
import_now --delete --apply > "$WORK/bulk.log" 2>&1
BULK=$(sql2 "SELECT COUNT(*) FROM ipaddresses;")
if [ "$BULK" -gt 50 ]; then
    pass "bulked the replica up to $BULK addresses so the limit is meaningful"
else
    fail "could not bulk up the replica (only $BULK addresses) -- the next check would prove nothing"
fi

# Now delete them all upstream: a snapshot that would wipe most of the
# replica. This is exactly what the limit exists to stop.
sql1 "DELETE FROM ipaddresses WHERE subnetId=$SRC30;" >/dev/null
export_now >/dev/null 2>&1
ship
import_now --delete --apply > "$WORK/masskill.log" 2>&1
RC=$?
check "mass deletion is refused" "1" "$RC"
grep -q "safety limit" "$WORK/masskill.log" \
    && pass "refusal names the safety limit" || fail "no safety-limit message"
grep -q "snapshot" "$WORK/masskill.log" \
    && pass "refusal points at the likely cause" || fail "refusal gives no diagnosis"
REMAINING=$(sql2 "SELECT COUNT(*) FROM ipaddresses;")
check "replica completely untouched by the refused run" "$BULK" "$REMAINING"

import_now --delete --apply --force-delete > "$WORK/forced.log" 2>&1
check "--force-delete overrides the limit" "0" "$?"
AFTER=$(sql2 "SELECT COUNT(*) FROM ipaddresses;")
if [ "$AFTER" -lt "$BULK" ]; then
    pass "forced run really did delete them ($BULK -> $AFTER)"
else
    fail "--force-delete did not delete anything ($BULK -> $AFTER)"
fi

say "14. a corrupted snapshot is refused"
python3 - "$WORK/side2-data" <<'PY'
import pathlib, sys
p = next(pathlib.Path(sys.argv[1]).glob("sections/*/*.json"))
p.write_text(p.read_text().replace('"description"', '"tampered"', 1))
PY
import_now --apply > "$WORK/corrupt.log" 2>&1
RC=$?
check "corrupt snapshot exits non-zero" "1" "$RC"
grep -q "Checksum mismatch" "$WORK/corrupt.log" \
    && pass "checksum mismatch detected and refused" || fail "corruption not detected"

if [ "$FAILURES" -eq 0 ]; then
    printf '\n\033[32mALL CHECKS PASSED\033[0m\n'
else
    printf '\n\033[31m%s CHECK(S) FAILED\033[0m\n' "$FAILURES"
fi
exit $((FAILURES > 0))
