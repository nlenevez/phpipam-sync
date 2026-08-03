#!/usr/bin/env bash
#
# Verifies what happens when the git mirror fails for a fortnight while
# the source keeps changing, and then comes back.
#
#   export PHPIPAM_SRC_TOKEN=... PHPIPAM_DST_TOKEN=...
#   ./verify-catchup.sh              # re-seeds, then verifies
#   ./verify-catchup.sh --no-setup
#
# The concern is that the target is hit by "a heap of changes from lots of
# files" all at once. The claim being tested is that it is not, in any
# meaningful sense, because the importer is STATE-BASED, not event-based:
# it reads the snapshot tree at HEAD, never the commit history. Fourteen
# days of commits therefore collapse into a single reconciliation against
# the final state, sized by the NET difference rather than by how many
# commits or intermediate edits occurred.
#
# Checks, in order:
#   1. baseline sync, target matches source
#   2. 14 days of change on the source while the mirror is "down"
#   3. the target is many commits behind and has not moved
#   4. one catch-up run converges it completely
#   5. the work done matches the NET diff, not the number of commits
#   6. records created-then-deleted during the outage never appear at all
#   7. an immediate re-run is a no-op (no repeated churn)
#   8. with deletes enabled, a big backlog of deletions trips the safety
#      limit rather than silently applying

set -uo pipefail
cd "$(dirname "$0")"
TOOL=$(cd .. && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

: "${PHPIPAM_SRC_TOKEN:?set PHPIPAM_SRC_TOKEN}"
: "${PHPIPAM_DST_TOKEN:?set PHPIPAM_DST_TOKEN}"

if [ "${1:-}" != "--no-setup" ]; then
    printf '\n\033[1m== re-seeding the lab\033[0m\n'
    ./setup.sh >/dev/null 2>&1 || { echo "setup.sh failed"; exit 1; }
fi

FAILURES=0
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILURES=$((FAILURES+1)); }
check(){ if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi; }
sql1() { docker compose exec -T db1 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }
sql2() { docker compose exec -T db2 mariadb -uroot -prootpw -N -B phpipam -e "$1" 2>/dev/null; }

export PYTHONWARNINGS=ignore
SUBNETS=20 PER_SUBNET=100 DAYS=14 CHANGES_PER_DAY=50

say "seeding the source with a realistic estate"
python3 - "$SUBNETS" "$PER_SUBNET" > "$WORK/seed.sql" <<'PY'
import sys, ipaddress as ipa
subnets, per = int(sys.argv[1]), int(sys.argv[2])
d = lambda a: str(int(ipa.ip_address(a)))
print("USE phpipam;")
sid, aid, rows = 3000, 30000, []
for i in range(subnets):
    net = f"10.60.{i}.0"
    print(f"INSERT INTO subnets (id,subnet,mask,sectionId,description,masterSubnetId,state) "
          f"VALUES ({sid},'{d(net)}','24',1,'catchup subnet {i}',0,2);")
    base = int(ipa.ip_address(net))
    for h in range(1, per + 1):
        rows.append(f"({aid},{sid},'{base+h}','host-{i}-{h}','baseline',0,'netops',2)")
        aid += 1
    sid += 1
for c in range(0, len(rows), 500):
    print("INSERT INTO ipaddresses (id,subnetId,ip_addr,hostname,description,is_gateway,owner,state) VALUES "
          + ",".join(rows[c:c+500]) + ";")
PY
docker compose exec -T db1 mariadb -uroot -prootpw < "$WORK/seed.sql"
# isFolder=0: setup.sh also seeds folders in this section, and they are
# not subnets.
check "source seeded" "$((SUBNETS + 4))" \
      "$(sql1 'SELECT COUNT(*) FROM subnets WHERE sectionId=1 AND isFolder=0;')"

git init -q --bare "$WORK/mirror.git"
git clone -q "$WORK/mirror.git" "$WORK/src"      # source side
git clone -q "$WORK/mirror.git" "$WORK/dst"      # target side (the "mirror")
git -C "$WORK/src" config user.email l@l; git -C "$WORK/src" config user.name l

cat > "$WORK/c1.yml" <<'EOF'
source: {base_url: "https://127.0.0.1:8443", app_id: sync, token_env: PHPIPAM_SRC_TOKEN, verify_ssl: false}
sections: [Shared]
EOF
cat > "$WORK/c2.yml" <<'EOF'
target: {base_url: "https://127.0.0.1:8444", app_id: sync, token_env: PHPIPAM_DST_TOKEN, verify_ssl: false}
sections: [Shared]
EOF
exp() { python3 "$TOOL/ipam_export.py" --config "$WORK/c1.yml" --out-dir "$WORK/src" "$@"; }
imp() { python3 "$TOOL/ipam_import.py" --config "$WORK/c2.yml" --snapshot-dir "$WORK/dst" "$@"; }
push_src() { git -C "$WORK/src" add -A; git -C "$WORK/src" commit -q -m "$1" >/dev/null 2>&1; \
             git -C "$WORK/src" push -q origin HEAD:main; }

say "1. baseline sync"
exp >/dev/null 2>&1; push_src baseline
git -C "$WORK/dst" pull -q --ff-only origin main
imp --apply > "$WORK/base.log" 2>&1
check "baseline exit status" "0" "$?"
BASE_ADDR=$(sql2 'SELECT COUNT(*) FROM ipaddresses;')
check "target matches source on addresses" "$(sql1 'SELECT COUNT(*) FROM ipaddresses a JOIN subnets s ON s.id=a.subnetId WHERE s.sectionId=1;')" "$BASE_ADDR"

say "2. the mirror fails for $DAYS days while the source keeps changing"
# The target simply never pulls. The source exports and commits daily.
CREATED_THEN_DELETED="10.60.0.250"
for day in $(seq 1 $DAYS); do
    sql1 "UPDATE ipaddresses SET hostname=CONCAT('host-r',$day,'-',id), description='changed day $day'
          WHERE id BETWEEN $((30000 + (day-1)*CHANGES_PER_DAY)) AND $((30000 + day*CHANGES_PER_DAY - 1));" >/dev/null
    if [ "$day" = 3 ]; then   # a record that appears and disappears mid-outage
        sql1 "INSERT INTO ipaddresses (subnetId,ip_addr,hostname,description,is_gateway,owner,state)
              VALUES (3000,INET_ATON('$CREATED_THEN_DELETED'),'transient','born mid-outage',0,'netops',2);" >/dev/null
    fi
    if [ "$day" = 9 ]; then
        sql1 "DELETE FROM ipaddresses WHERE ip_addr=INET_ATON('$CREATED_THEN_DELETED');" >/dev/null
    fi
    exp >/dev/null 2>&1; push_src "day $day"
done
BEHIND=$(git -C "$WORK/dst" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
git -C "$WORK/dst" fetch -q origin
BEHIND=$(git -C "$WORK/dst" rev-list --count HEAD..origin/main)
if [ "$BEHIND" -ge 10 ]; then
    pass "target is $BEHIND commits behind"
else
    fail "expected a real backlog of commits, got $BEHIND"
fi

say "3. the target has not moved while the mirror was down"
check "target address count unchanged during outage" "$BASE_ADDR" "$(sql2 'SELECT COUNT(*) FROM ipaddresses;')"

say "4. one catch-up run converges it"
START=$(date +%s)
git -C "$WORK/dst" pull -q --ff-only origin main
imp --apply > "$WORK/catchup.log" 2>&1
RC=$?; ELAPSED=$(( $(date +%s) - START ))
check "catch-up exit status" "0" "$RC"
printf '  \033[36mINFO\033[0m catch-up took %ss for %s commits of backlog\n' "$ELAPSED" "$BEHIND"

SRC_HOSTS=$(sql1 "SELECT GROUP_CONCAT(hostname ORDER BY ip_addr) FROM ipaddresses a JOIN subnets s ON s.id=a.subnetId WHERE s.sectionId=1;" | md5sum | cut -d' ' -f1)
DST_HOSTS=$(sql2 "SELECT GROUP_CONCAT(hostname ORDER BY ip_addr) FROM ipaddresses;" | md5sum | cut -d' ' -f1)
check "target now exactly matches source (hostname digest)" "$SRC_HOSTS" "$DST_HOSTS"

say "5. the work done matches the NET diff, not the commit count"
APPLIED=$(grep -oE "applied [0-9]+ change" "$WORK/catchup.log" | grep -oE "[0-9]+")
EXPECTED_MAX=$((DAYS * CHANGES_PER_DAY))
printf '  \033[36mINFO\033[0m applied %s change(s) after %s commits / %s edits upstream\n' \
       "$APPLIED" "$BEHIND" "$EXPECTED_MAX"
if [ "$APPLIED" -le "$EXPECTED_MAX" ]; then
    pass "work is bounded by the net difference ($APPLIED <= $EXPECTED_MAX)"
else
    fail "applied more changes ($APPLIED) than edits made upstream ($EXPECTED_MAX)"
fi

say "6. records created AND deleted during the outage never appear"
# The target only ever sees final state, so churn that cancelled out
# upstream costs it nothing at all.
check "transient record never reached the target" "0" \
      "$(sql2 "SELECT COUNT(*) FROM ipaddresses WHERE ip_addr=INET_ATON('$CREATED_THEN_DELETED');")"
if grep -q "$CREATED_THEN_DELETED" "$WORK/catchup.log"; then
    fail "the transient record was mentioned in the catch-up plan"
else
    pass "it is not even mentioned in the plan"
fi

say "7. an immediate re-run is a no-op"
imp --apply > "$WORK/after.log" 2>&1
grep -q "changes: none" "$WORK/after.log" \
    && pass "no repeated churn after catch-up" \
    || fail "re-run was not clean: $(grep -E '^summary' "$WORK/after.log")"

say "8. a big DELETION backlog trips the safety limit instead of applying"
# Same outage shape, but the change is mass deletion upstream. With
# strict mirror on, catching up would remove a large share of the target
# in one go -- which is exactly what the limit exists to stop.
sql1 "DELETE FROM ipaddresses WHERE subnetId IN (SELECT id FROM subnets WHERE sectionId=1 AND description LIKE 'catchup%');" >/dev/null
exp >/dev/null 2>&1; push_src "mass delete"
git -C "$WORK/dst" pull -q --ff-only origin main
BEFORE=$(sql2 'SELECT COUNT(*) FROM ipaddresses;')
imp --delete --apply > "$WORK/masskill.log" 2>&1
check "catch-up with a huge delete backlog is refused" "1" "$?"
grep -q "safety limit" "$WORK/masskill.log" \
    && pass "refusal names the safety limit" || fail "no safety-limit message"
check "target untouched by the refused run" "$BEFORE" "$(sql2 'SELECT COUNT(*) FROM ipaddresses;')"
printf '  \033[36mINFO\033[0m this is by design: after a long outage, review before --force-delete\n'

if [ "$FAILURES" -eq 0 ]; then
    printf '\n\033[32mALL CATCH-UP CHECKS PASSED\033[0m\n'
else
    printf '\n\033[31m%s CHECK(S) FAILED\033[0m\n' "$FAILURES"
fi
exit $((FAILURES > 0))
