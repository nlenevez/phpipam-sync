#!/usr/bin/env bash
#
# Verifies the FAN-IN topology: several airgapped subordinates, each
# managing their own networks, pushing up into one read-only master.
#
#   export PHPIPAM_SRC_TOKEN=... PHPIPAM_DST_TOKEN=...
#   ./verify-fanin.sh                # re-seeds, then verifies
#   ./verify-fanin.sh --no-setup
#
# Instances 1 and 2 act as subordinates A and B (each seeded with its own
# non-overlapping networks); instance 3 is the master. Each subordinate
# pushes to its OWN mirrored repo, and the master imports both in a
# single run from one config.
#
# The property this exists to prove is the dangerous one: subordinate A's
# import must not touch subordinate B's records on the master. Every
# import treats anything in its target section that is absent from its
# snapshot as an orphan -- reported as drift, and deleted once
# delete_drift is on. If the two ever shared a section, A's sync would
# wipe B's networks. So this checks both that the section-per-source
# layout keeps them apart, AND that a config which would share a section
# is refused outright.

set -uo pipefail
cd "$(dirname "$0")"
TOOL=$(cd .. && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

: "${PHPIPAM_SRC_TOKEN:?set PHPIPAM_SRC_TOKEN (see setup.sh output)}"
: "${PHPIPAM_DST_TOKEN:?set PHPIPAM_DST_TOKEN (see setup.sh output)}"

if [ "${1:-}" != "--no-setup" ]; then
    printf '\n\033[1m== re-seeding the lab\033[0m\n'
    ./setup.sh >/dev/null 2>&1 || { echo "setup.sh failed; run it directly"; exit 1; }
fi

FAILURES=0
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILURES=$((FAILURES+1)); }
check(){ if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi; }
sqlA() { docker compose exec -T db1 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }
sqlB() { docker compose exec -T db2 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }
sqlM() { docker compose exec -T db3 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }

export PYTHONWARNINGS=ignore

say "seeding subordinate B with its own, non-overlapping networks"
# Instance 2 is the 1:1 lab's replica; re-purpose it as a second
# subordinate with a distinct section name and distinct address space.
docker compose exec -T db2 mariadb -uroot -prootpw phpipam <<'EOF'
SET FOREIGN_KEY_CHECKS=0;
DELETE FROM ipaddresses; DELETE FROM subnets; DELETE FROM sections;
SET FOREIGN_KEY_CHECKS=1;
INSERT INTO sections (id,name,description,permissions,strictMode,`order`)
  VALUES (700,'Networks','Site B networks','{"3":"1"}',1,1);
INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,state)
  VALUES (800,INET_ATON('172.16.0.0'),'16',700,'Site B supernet',0,2);
INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,state)
  VALUES (801,INET_ATON('172.16.5.0'),'24',700,'Site B /24',800,2);
INSERT INTO ipaddresses (id,subnetId,ip_addr,hostname,description,is_gateway,owner,state)
  VALUES (8500,801,INET_ATON('172.16.5.1'),'b-gw','site B gateway',1,'site-b',2),
         (8501,801,INET_ATON('172.16.5.10'),'b-sw10','site B switch',0,'site-b',2);
EOF
# Subordinate A exports its section as "Shared"; give B's a different
# name too, so the section_map is genuinely doing work.
check "subordinate B seeded" "2" "$(sqlB 'SELECT COUNT(*) FROM subnets;')"

say "building one mirrored repo per subordinate"
for site in a b; do
    git init -q --bare "$WORK/site-$site.git"
    git clone -q "$WORK/site-$site.git" "$WORK/data-$site"
    git -C "$WORK/data-$site" config user.email lab@lab
    git -C "$WORK/data-$site" config user.name lab
done

cat > "$WORK/site-a.yml" <<EOF
source: {base_url: "https://127.0.0.1:8443", app_id: sync, token_env: PHPIPAM_SRC_TOKEN, verify_ssl: false}
sections: [Shared]
EOF
cat > "$WORK/site-b.yml" <<EOF
source: {base_url: "https://127.0.0.1:8444", app_id: sync, token_env: PHPIPAM_DST_TOKEN, verify_ssl: false}
sections: [Networks]
EOF
# The master: ONE config, both subordinates, one section each.
cat > "$WORK/master.yml" <<EOF
target: {base_url: "https://127.0.0.1:8445", app_id: sync, token_env: PHPIPAM_DST_TOKEN, verify_ssl: false}
sources:
  - name: site-a
    snapshot_dir: $WORK/data-a
    sections: [Shared]
    section_map: {Shared: Site-A}
  - name: site-b
    snapshot_dir: $WORK/data-b
    sections: [Networks]
    section_map: {Networks: Site-B}
EOF

export_site() { # export_site <a|b> <token-var>
    python3 "$TOOL/ipam_export.py" --config "$WORK/site-$1.yml" --out-dir "$WORK/data-$1" "${@:3}"
}
master() { python3 "$TOOL/ipam_import.py" --config "$WORK/master.yml" "$@"; }
ship() {
    git -C "$WORK/data-$1" add -A
    git -C "$WORK/data-$1" commit -q -m snapshot >/dev/null 2>&1
    git -C "$WORK/data-$1" push -q origin HEAD:main
}

say "1. each subordinate exports to its own repo"
export_site a > "$WORK/exp-a.log" 2>&1 || fail "site-a export failed"
export_site b > "$WORK/exp-b.log" 2>&1 || fail "site-b export failed"
ship a; ship b
check "site-a snapshot has its subnets" "4" \
      "$(python3 -c 'import json;print(json.load(open("'"$WORK"'/data-a/manifest.json"))["subnet_count"])')"
check "site-b snapshot has its subnets" "2" \
      "$(python3 -c 'import json;print(json.load(open("'"$WORK"'/data-b/manifest.json"))["subnet_count"])')"

say "2. the master imports BOTH subordinates in one run"
master --apply > "$WORK/master.log" 2>&1
check "master run exit status" "0" "$?"
grep -q "2 source(s)" "$WORK/master.log" \
    && pass "both sources processed in one invocation" \
    || fail "expected a 2-source run: $(grep -E '^mode' "$WORK/master.log")"
grep -q "\[site-a\]" "$WORK/master.log" && grep -q "\[site-b\]" "$WORK/master.log" \
    && pass "output is labelled per source" || fail "per-source labels missing"

say "3. each site's networks land in its own section"
A_SEC=$(sqlM "SELECT id FROM sections WHERE name='Site-A';")
B_SEC=$(sqlM "SELECT id FROM sections WHERE name='Site-B';")
check "Site-A holds subordinate A's supernet" "1" \
      "$(sqlM "SELECT COUNT(*) FROM subnets WHERE sectionId=$A_SEC AND subnet=INET_ATON('10.20.0.0');")"
check "Site-B holds subordinate B's supernet" "1" \
      "$(sqlM "SELECT COUNT(*) FROM subnets WHERE sectionId=$B_SEC AND subnet=INET_ATON('172.16.0.0');")"
check "no site-B networks leaked into Site-A" "0" \
      "$(sqlM "SELECT COUNT(*) FROM subnets WHERE sectionId=$A_SEC AND subnet=INET_ATON('172.16.0.0');")"
check "no site-A networks leaked into Site-B" "0" \
      "$(sqlM "SELECT COUNT(*) FROM subnets WHERE sectionId=$B_SEC AND subnet=INET_ATON('10.20.0.0');")"
check "master carries both sites' addresses" "7" \
      "$(sqlM 'SELECT COUNT(*) FROM ipaddresses;')"

say "4. the master run is idempotent"
master --apply > "$WORK/master2.log" 2>&1
if grep -q "changes: none" "$WORK/master2.log" && ! grep -qE "^\[.*\] applied [1-9]" "$WORK/master2.log"; then
    pass "second master run makes no changes"
else
    fail "master run not idempotent: $(grep -E 'summary' "$WORK/master2.log" | tr '\n' ' ')"
fi

say "5. a subordinate NEVER reports or touches another's records"
# The core safety property. With deletion ON, site-a's import must still
# leave every one of site-b's records alone.
B_BEFORE=$(sqlM "SELECT COUNT(*) FROM subnets WHERE sectionId=$B_SEC;")
master --source site-a --delete --apply > "$WORK/only-a.log" 2>&1
check "site-a-only run exit status" "0" "$?"
check "site-b's subnets untouched by site-a's run" "$B_BEFORE" \
      "$(sqlM "SELECT COUNT(*) FROM subnets WHERE sectionId=$B_SEC;")"
if grep -qE "172\.16\." "$WORK/only-a.log"; then
    fail "site-a's run mentioned site-b's networks -- scoping is wrong"
else
    pass "site-a's run never even mentions site-b's networks"
fi

say "6. a config that would share a section is refused"
cat > "$WORK/bad.yml" <<EOF
target: {base_url: "https://127.0.0.1:8445", app_id: sync, token_env: PHPIPAM_DST_TOKEN, verify_ssl: false}
sources:
  - name: site-a
    snapshot_dir: $WORK/data-a
    sections: [Shared]
    section_map: {Shared: Everything}
  - name: site-b
    snapshot_dir: $WORK/data-b
    sections: [Networks]
    section_map: {Networks: Everything}
EOF
python3 "$TOOL/ipam_import.py" --config "$WORK/bad.yml" > "$WORK/bad.log" 2>&1
check "shared-section config exits non-zero" "1" "$?"
grep -q "both target the section" "$WORK/bad.log" \
    && pass "refusal names the colliding section" || fail "no collision message"
grep -q "DELETED" "$WORK/bad.log" \
    && pass "refusal explains the consequence" || fail "refusal does not explain why"

say "7. one broken subordinate does not stop the others"
# An airgapped site's mirror can go stale or arrive corrupt. The master
# must still sync everyone else.
python3 - "$WORK/data-b" <<'PY'
import pathlib, sys
p = next(pathlib.Path(sys.argv[1]).glob("sections/*/*.json"))
p.write_text(p.read_text().replace('"description"', '"broken"', 1))
PY
sqlA "UPDATE subnets SET description='changed at site A' WHERE id=12;" >/dev/null
export_site a >/dev/null 2>&1; ship a
master --apply > "$WORK/partial.log" 2>&1
RC=$?
check "run reports failure when a source is broken" "1" "$RC"
grep -q "Checksum mismatch" "$WORK/partial.log" \
    && pass "the broken source is diagnosed" || fail "no diagnosis for the broken source"
check "the healthy source still synced" "changed at site A" \
      "$(sqlM "SELECT description FROM subnets WHERE sectionId=$A_SEC AND subnet=INET_ATON('10.20.5.0');")"
grep -q "1/2 source(s) processed" "$WORK/partial.log" \
    && pass "summary counts processed vs failed sources" \
    || fail "no per-run source tally: $(grep -E '^==' "$WORK/partial.log")"

if [ "$FAILURES" -eq 0 ]; then
    printf '\n\033[32mALL FAN-IN CHECKS PASSED\033[0m\n'
else
    printf '\n\033[31m%s CHECK(S) FAILED\033[0m\n' "$FAILURES"
fi
exit $((FAILURES > 0))
