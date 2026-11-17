#!/bin/bash
# 3-way merge upstream phpIPAM changes into a locally modified install.
# Needs both pristine trees; apply-upgrade.sh needs only the patch file.
#
#   BASE   = pristine tree of the version you run   (e.g. 1.8.1)
#   NEW    = pristine tree of the target version    (e.g. 1.8.2)
#   TARGET = your live install
#
# Only files changed between BASE and NEW are touched. Local changes are
# kept; collisions are left with conflict markers and reported.
#
# Usage: upgrade-3way.sh <BASE> <NEW> <TARGET> [--apply]
# Without --apply: dry run, writes nothing.

set -u

BASE=${1:?base tree}; NEW=${2:?new tree}; TARGET=${3:?target install}
APPLY=0; [ "${4:-}" = "--apply" ] && APPLY=1

for d in "$BASE" "$NEW" "$TARGET"; do
    [ -d "$d" ] || { echo "ERROR: not a directory: $d" >&2; exit 2; }
done

# Pick a 3-way merge engine.
if command -v git >/dev/null 2>&1; then
    MERGE=git
elif command -v diff3 >/dev/null 2>&1; then
    MERGE=diff3
else
    echo "ERROR: need 'git' or 'diff3' for a 3-way merge (dnf install git-core diffutils)" >&2
    exit 2
fi

n_same=0 n_clean=0 n_merged=0 n_conflict=0 n_missing=0 n_added=0 n_removed=0
conflicts=(); locals=(); missing=()

# Files that changed between BASE and NEW.
while IFS= read -r f; do
    b="$BASE/$f"; n="$NEW/$f"; t="$TARGET/$f"

    # Added upstream.
    if [ ! -e "$b" ] && [ -e "$n" ]; then
        n_added=$((n_added+1))
        echo "ADD      $f"
        [ $APPLY -eq 1 ] && { mkdir -p "$(dirname "$t")"; cp -p "$n" "$t"; }
        continue
    fi
    # Removed upstream.
    if [ -e "$b" ] && [ ! -e "$n" ]; then
        n_removed=$((n_removed+1))
        echo "REMOVE   $f   (not deleted automatically - review by hand)"
        continue
    fi
    # Present upstream but absent from the install.
    if [ ! -e "$t" ]; then
        n_missing=$((n_missing+1)); missing+=("$f")
        echo "MISSING  $f   (absent from your install - copying it in)"
        [ $APPLY -eq 1 ] && { mkdir -p "$(dirname "$t")"; cp -p "$n" "$t"; }
        continue
    fi

    if cmp -s "$t" "$n"; then
        n_same=$((n_same+1)); continue          # already at the new version
    fi

    if cmp -s "$t" "$b"; then
        # Unmodified locally: copy upstream's file.
        n_clean=$((n_clean+1))
        echo "UPDATE   $f"
        [ $APPLY -eq 1 ] && cp -p "$n" "$t"
        continue
    fi

    # Locally modified AND changed upstream -> 3-way merge.
    locals+=("$f")
    tmp=$(mktemp)
    cp "$t" "$tmp"
    if [ "$MERGE" = git ]; then
        git merge-file -L "yours ($f)" -L "phpIPAM base" -L "phpIPAM new" \
            "$tmp" "$b" "$n" >/dev/null 2>&1
        rc=$?
    else
        diff3 -m -L "yours ($f)" -L "phpIPAM base" -L "phpIPAM new" \
            "$tmp" "$b" "$n" > "$tmp.out" 2>/dev/null
        rc=$?
        mv "$tmp.out" "$tmp"
    fi

    if [ $rc -eq 0 ]; then
        n_merged=$((n_merged+1))
        echo "MERGED   $f   (your changes kept, upstream changes applied)"
        [ $APPLY -eq 1 ] && cat "$tmp" > "$t"
    else
        n_conflict=$((n_conflict+1)); conflicts+=("$f")
        echo "CONFLICT $f   <-- needs manual resolution"
        [ $APPLY -eq 1 ] && cat "$tmp" > "$t"
    fi
    rm -f "$tmp"
done < <(
    { cd "$BASE" && find . -type f -printf '%P\n'
      cd "$NEW"  && find . -type f -printf '%P\n'; } | sort -u |
    while IFS= read -r f; do
        cmp -s "$BASE/$f" "$NEW/$f" || echo "$f"
    done
)

echo
echo "================ SUMMARY ================"
echo "  already up to date : $n_same"
echo "  updated cleanly    : $n_clean       (untouched locally)"
echo "  merged             : $n_merged       (your changes preserved)"
echo "  CONFLICTS          : $n_conflict"
echo "  missing from install: $n_missing"
echo "  added upstream     : $n_added"
echo "  removed upstream   : $n_removed"
if [ ${#locals[@]} -gt 0 ]; then
    echo
    echo "Files where your modifications overlap this release:"
    printf '  %s\n' "${locals[@]}"
fi
if [ ${#conflicts[@]} -gt 0 ]; then
    echo
    echo "RESOLVE THESE (conflict markers '<<<<<<<' are in the files):"
    printf '  %s\n' "${conflicts[@]}"
fi
if [ $APPLY -eq 0 ]; then
    echo
    echo "DRY RUN - nothing was written. Re-run with --apply to make changes."
fi
[ $n_conflict -gt 0 ] && exit 1
exit 0
