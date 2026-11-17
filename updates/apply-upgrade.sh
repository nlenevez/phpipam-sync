#!/bin/bash
# Apply an upstream phpIPAM patch to a locally modified install. Needs only
# the .patch file, no pristine trees.
#
#   ./apply-upgrade.sh /var/www/phpipam phpipam-1.8.1-to-1.8.2.patch [--apply]
#
# Without --apply: dry run, writes nothing.
#
# Non-colliding local changes are kept. Colliding hunks leave conflict
# markers and are reported; plain `patch -p1` would silently drop them.

set -uo pipefail

TARGET=${1:?path to the phpIPAM install}
PATCHFILE=${2:?path to the .patch}
APPLY=0; [ "${3:-}" = "--apply" ] && APPLY=1

[ -d "$TARGET" ]    || { echo "ERROR: not a directory: $TARGET" >&2; exit 2; }
[ -r "$PATCHFILE" ] || { echo "ERROR: cannot read: $PATCHFILE" >&2; exit 2; }
command -v patch >/dev/null || { echo "ERROR: 'patch' not installed (dnf install patch)" >&2; exit 2; }

PATCHFILE=$(readlink -f "$PATCHFILE")

# --merge                   diff3-style merge; conflict markers, not .rej files
# --no-backup-if-mismatch   no .orig files: a web server serves *.php.orig as source
# --forward                 skip hunks that are already applied (idempotent)
PATCH_OPTS="-p1 --merge --no-backup-if-mismatch --forward"

echo "== target : $TARGET"
echo "== patch  : $PATCHFILE"
echo "== files  : $(grep -c '^diff -Naur' "$PATCHFILE") changed upstream"
echo

if [ $APPLY -eq 0 ]; then
    echo "== DRY RUN =="
    # Track the current filename so "NOT MERGED" lines can be attributed.
    ( cd "$TARGET" && patch $PATCH_OPTS --dry-run < "$PATCHFILE" ) 2>&1 | awk '
        /^(checking|patching) file / { f=$3; next }
        /NOT MERGED/      { print "  CONFLICT  " f " (" $0 ")"; c++; next }
        /already applied/ { if (!seen[f]++) print "  ALREADY   " f; a++; next }
        /FAILED/          { print "  FAILED    " f " (" $0 ")"; x++; next }
        END {
            print ""
            print "  would conflict      : " c+0
            print "  already applied     : " a+0
            print "  would fail outright : " x+0
            if (c+0 == 0 && x+0 == 0) print "  -> clean: every upstream change applies without touching your edits"
        }'
    echo
    echo "Nothing was written. Re-run with --apply to make changes."
    echo "TAKE A BACKUP FIRST:  tar czf /var/tmp/phpipam-pre-upgrade.tgz -C $TARGET ."
    exit 0
fi

echo "== APPLYING =="
( cd "$TARGET" && patch $PATCH_OPTS < "$PATCHFILE" )
rc=$?
echo "   patch exit=$rc"

# The conflict markers themselves are the authoritative conflict list.
mapfile -t conflicted < <(grep -rl '^<<<<<<<' "$TARGET" \
    --include='*.php' --include='*.js' --include='*.css' --include='*.sql' 2>/dev/null)

# .orig/.rej leftovers in a web root expose PHP source.
mapfile -t leftovers < <(find "$TARGET" \( -name '*.orig' -o -name '*.rej' \) 2>/dev/null)

echo
echo "================ RESULT ================"
if [ ${#conflicted[@]} -eq 0 ]; then
    echo "  conflicts : none"
else
    echo "  conflicts : ${#conflicted[@]} - RESOLVE THESE BEFORE SERVING TRAFFIC"
    printf '    %s\n' "${conflicted[@]}"
    echo
    echo "  Each conflict looks like:"
    echo "      <<<<<<<"
    echo "      your version"
    echo "      ======="
    echo "      upstream version"
    echo "      >>>>>>>"
    echo "  Keep both where you can - the upstream side is usually a security fix."
fi

if [ ${#leftovers[@]} -gt 0 ]; then
    echo
    echo "  WARNING: patch leftovers in the docroot (nginx serves these as SOURCE):"
    printf '    %s\n' "${leftovers[@]}"
    echo "  Remove them:  find $TARGET \\( -name '*.orig' -o -name '*.rej' \\) -delete"
fi

# Syntax-check patched files; catches unresolved markers and bad resolutions.
if command -v php >/dev/null; then
    echo
    echo "== php -l on the files this patch touched =="
    bad=0
    while IFS= read -r f; do
        [ -f "$TARGET/$f" ] || continue
        case "$f" in *.php) ;; *) continue ;; esac
        if ! out=$(php -l "$TARGET/$f" 2>&1); then
            echo "  SYNTAX ERROR: $f"
            echo "$out" | head -2 | sed 's/^/      /'
            bad=$((bad+1))
        fi
    done < <(grep '^diff -Naur' "$PATCHFILE" | awk '{print $4}' | sed 's|^b/||')
    [ $bad -eq 0 ] && echo "  all clean" || echo "  $bad file(s) do not parse - DO NOT SERVE"
else
    echo
    echo "  NOTE: php not on PATH, syntax check skipped. Run it by hand:"
    echo "    find $TARGET -name '*.php' | xargs -n1 php -l | grep -v 'No syntax'"
fi

echo
echo "Then bump the DB version (no schema change in 1.8.2):"
echo "  log in as admin and run /upgrade/, or:  UPDATE settings SET version='1.82';"

[ ${#conflicted[@]} -gt 0 ] && exit 1
exit 0
