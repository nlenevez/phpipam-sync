#!/bin/bash
# Generate an upstream-only patch between two phpIPAM releases. Run with
# internet access; carry the .patch to the airgapped host.
#
#   ./make-patch.sh 1.8.1 1.8.2
#     -> phpipam-1.8.1-to-1.8.2.patch
#     -> pristine trees in ./phpipam-1.8.1/ and ./phpipam-1.8.2/
#        (upgrade-3way.sh needs both)
#
# Built from release tarballs, not git: releases add a GITVERSION define to
# functions/version.php that git trees lack.

set -euo pipefail

OLD=${1:?old version, e.g. 1.8.1}
NEW=${2:?new version, e.g. 1.8.2}
BASEURL=https://github.com/phpipam/phpipam/releases/download

fetch() {
    local v=$1
    if [ -d "phpipam-$v" ]; then
        echo "  phpipam-$v/ already present, reusing"
        return
    fi
    echo "  downloading phpipam-v$v.tgz"
    curl -fsSL -o "phpipam-v$v.tgz"   "$BASEURL/v$v/phpipam-v$v.tgz"
    curl -fsSL -o "SHA256-$v.txt"     "$BASEURL/v$v/SHA256.txt"

    # Verify against upstream's SHA256.txt.
    local want have
    want=$(awk -v f="phpipam-v$v.tgz" '$2==f || $2=="*"f {print $1}' "SHA256-$v.txt" | head -1)
    have=$(sha256sum "phpipam-v$v.tgz" | cut -d' ' -f1)
    if [ -z "$want" ]; then
        echo "  WARNING: no checksum for phpipam-v$v.tgz in SHA256.txt" >&2
    elif [ "$want" != "$have" ]; then
        echo "  ERROR: checksum mismatch for phpipam-v$v.tgz" >&2
        echo "    expected $want" >&2
        echo "    got      $have" >&2
        exit 1
    else
        echo "  checksum OK ($have)"
    fi

    local tmp; tmp=$(mktemp -d)
    tar xzf "phpipam-v$v.tgz" -C "$tmp"
    # tarballs unpack to a single phpipam/ directory
    mv "$tmp/phpipam" "phpipam-$v"
    rmdir "$tmp"
}

echo "== fetching $OLD =="; fetch "$OLD"
echo "== fetching $NEW =="; fetch "$NEW"

OUT="phpipam-$OLD-to-$NEW.patch"
HERE=$(pwd)
echo "== generating $OUT =="
# diff exits 1 when files differ.
diff -Naur "phpipam-$OLD" "phpipam-$NEW" > "$OUT" || true

# Rewrite the path prefixes to a/ and b/ so the patch applies with -p1.
sed -i -e "s|^--- phpipam-$OLD/|--- a/|" \
       -e "s|^+++ phpipam-$NEW/|+++ b/|" \
       -e "s|^diff -Naur phpipam-$OLD/\(.*\) phpipam-$NEW/\(.*\)$|diff -Naur a/\1 b/\2|" "$OUT"

echo
echo "  files touched : $(grep -c '^diff -Naur' "$OUT")"
echo "  hunks         : $(grep -c '^@@' "$OUT")"
echo "  size          : $(stat -c%s "$OUT") bytes"

# Self-check: pristine OLD + patch must equal NEW byte for byte.
echo
echo "== self-check: pristine $OLD + patch == $NEW ? =="
CHK=$(mktemp -d)
cp -a "phpipam-$OLD" "$CHK/t"
( cd "$CHK/t" && patch -p1 --forward --silent < "$HERE/$OUT" )
if diff -rq "$CHK/t" "phpipam-$NEW" > /dev/null; then
    echo "  PASS - patched tree is byte-identical to phpipam-$NEW"
    rm -rf "$CHK"
else
    echo "  FAIL - patched tree differs from phpipam-$NEW:" >&2
    diff -rq "$CHK/t" "phpipam-$NEW" | head -20 >&2
    echo "  (left for inspection in $CHK)" >&2
    exit 1
fi

echo
echo "Carry to the airgapped host:"
echo "  $OUT"
echo "  phpipam-$OLD/   and   phpipam-$NEW/   (needed for the 3-way merge)"
