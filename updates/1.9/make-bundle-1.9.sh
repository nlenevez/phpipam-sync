#!/bin/bash
# Build the offline 1.8.1/1.8.2 -> 1.9.0 upgrade bundle. Run with internet
# access; needs curl, tar, php >= 8.2, composer, git.
#
#   ./make-bundle-1.9.sh [workdir]
#     -> phpipam-upgrade-1.9.0.tgz + .sha256
#
# Bundle contents:
#   apply-upgrade-1.9.sh   run this on the target host
#   phpipam-1.9.0/         release tarball + vendor/ from composer.lock
#   base/1.8.1, base/1.8.2 pristine trees (change detection, merge base)
#   tools/                 Rector + Pint as pinned by 1.9.0, plus Rector 2.1.0
#
# Tarballs are checked against upstream's SHA256.txt.

set -euo pipefail

HERE=$(dirname "$(readlink -f "$0")")
WORK=$(readlink -m "${1:-$PWD/bundle-work}")
OUTNAME=phpipam-upgrade-1.9.0
BASEURL=https://github.com/phpipam/phpipam/releases/download

for c in curl tar php composer git; do command -v $c >/dev/null || { echo "ERROR: need $c" >&2; exit 2; }; done
php -r 'exit(PHP_VERSION_ID >= 80200 ? 0 : 1);' || { echo "ERROR: need php >= 8.2 to run the tools" >&2; exit 2; }

mkdir -p "$WORK/dl"
B=$WORK/$OUTNAME
rm -rf "$B"; mkdir -p "$B/base" "$B/tools"

fetch() {
    local v=$1 d=$WORK/dl
    [ -f "$d/phpipam-v$v.tgz" ] || curl -fsSL -o "$d/phpipam-v$v.tgz" "$BASEURL/v$v/phpipam-v$v.tgz"
    curl -fsSL -o "$d/SHA256-$v.txt" "$BASEURL/v$v/SHA256.txt"
    local want have
    want=$(awk -v f="phpipam-v$v.tgz" '$2==f || $2=="*"f {print $1}' "$d/SHA256-$v.txt" | head -1)
    have=$(sha256sum "$d/phpipam-v$v.tgz" | cut -d' ' -f1)
    [ -n "$want" ] && [ "$want" = "$have" ] || { echo "ERROR: checksum mismatch for phpipam-v$v.tgz" >&2; exit 2; }
    echo "  phpipam-v$v.tgz  sha256 OK ($have)"
}

echo "== fetching releases"
for v in 1.8.1 1.8.2 1.9.0; do fetch $v; done

echo "== extracting"
for v in 1.8.1 1.8.2; do
    mkdir -p "$B/base/$v"; tar xzf "$WORK/dl/phpipam-v$v.tgz" -C "$B/base/$v" --strip-components=1
done
mkdir -p "$B/phpipam-1.9.0"; tar xzf "$WORK/dl/phpipam-v1.9.0.tgz" -C "$B/phpipam-1.9.0" --strip-components=1

echo "== composer install (no-dev) from 1.9.0's composer.lock"
# Target host extensions are checked by apply-upgrade-1.9.sh, not here.
( cd "$B/phpipam-1.9.0" && COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --no-scripts --no-interaction --ignore-platform-reqs --quiet )
[ -f "$B/phpipam-1.9.0/vendor/autoload.php" ] || { echo "ERROR: vendor/ not built" >&2; exit 2; }

echo "== tools: rector + pint at 1.9.0's locked versions"
read -r RECTOR_V PINT_V < <(php -r '$l=json_decode(file_get_contents($argv[1]),true); $v=[];
    foreach($l["packages-dev"] as $p) $v[$p["name"]]=ltrim($p["version"],"v");
    echo $v["rector/rector"]," ",$v["laravel/pint"],"\n";' "$B/phpipam-1.9.0/composer.lock")
echo "   rector/rector $RECTOR_V, laravel/pint $PINT_V"
cat > "$B/tools/composer.json" <<EOF
{
  "require": { "rector/rector": "$RECTOR_V", "laravel/pint": "$PINT_V" },
  "config":  { "platform": { "php": "8.2" } }
}
EOF
( cd "$B/tools" && COMPOSER_ALLOW_SUPERUSER=1 composer install --no-interaction --no-scripts --quiet )
cp "$HERE/rector-normalise.php" "$HERE/rector-casts.php" "$B/tools/"
# Rector 2.1.0 for NullToStrictStringFuncCallArgRector's (string) casts
# (2.1.7+ no longer adds them). Newer PHPStan breaks Rector 2.1.0.
mkdir -p "$B/tools/rector-2.1"
cat > "$B/tools/rector-2.1/composer.json" <<'EOF'
{
  "require": { "rector/rector": "2.1.0", "phpstan/phpstan": "2.1.17" },
  "config":  { "platform": { "php": "8.2" } }
}
EOF
( cd "$B/tools/rector-2.1" && COMPOSER_ALLOW_SUPERUSER=1 composer install --no-interaction --no-scripts --quiet )
cp "$HERE/apply-upgrade-1.9.sh" "$B/"; chmod +x "$B/apply-upgrade-1.9.sh"
cp "$HERE/README-bundle.md" "$B/README.md"

echo "== verifying the 1.8.x -> 1.9.0 path map against the tarballs"
# Every 1.8.x file must exist in 1.9.0 at map(f) or be a known removal.
eval "$(sed -n '/^map() {/,/^}/p' "$HERE/apply-upgrade-1.9.sh")"
for v in 1.8.1 1.8.2; do
    unexpected=$(cd "$B/base/$v" && find . -type f -printf '%P\n' | while IFS= read -r f; do
        [ -e "$B/phpipam-1.9.0/$(map "$f")" ] && continue
        case "$f" in
            functions/LdapRecord/*|functions/php-saml/*|functions/PHPMailer/*|functions/parsedown/*|\
            functions/xmlseclibs/*|functions/GoogleAuthenticator/*|functions/qrcodejs/*|app/login/captcha/*|\
            functions/composer.*|functions/include-only.php|functions/php_poly_fill.php|functions/classes/class.Radius.php|\
            UPDATE|db/bkp/*) ;;
            *) echo "$f" ;;
        esac
    done)
    if [ -n "$unexpected" ]; then
        echo "ERROR: $v files with no 1.9.0 counterpart that map() does not explain:" >&2
        echo "$unexpected" | head -20 >&2; exit 2
    fi
    echo "   $v: map OK"
done

echo "== self-test: pristine 1.8.x must upgrade to exactly 1.9.0 (+ config.php)"
for v in 1.8.1 1.8.2; do
    t=$WORK/selftest-$v; rm -rf "$t" "$t-new"
    cp -a "$B/base/$v" "$t"; cp "$t/config.dist.php" "$t/config.php"
    "$B/apply-upgrade-1.9.sh" "$t" "$t-new" > "$WORK/selftest-$v.log" 2>&1 || { echo "ERROR: self-test $v failed, see $WORK/selftest-$v.log" >&2; exit 2; }
    d=$(diff -rq --exclude=.upgrade-1.9 --exclude=config.php "$B/phpipam-1.9.0" "$t-new" || true)
    [ -z "$d" ] || { echo "ERROR: self-test $v: result differs from 1.9.0:" >&2; echo "$d" | head >&2; exit 2; }
    grep -q 'modified : 0' "$WORK/selftest-$v.log" || { echo "ERROR: self-test $v: pristine tree reported as modified" >&2; exit 2; }
    echo "   $v: identical to 1.9.0"
    rm -rf "$t" "$t-new"
done

echo "== packing"
( cd "$WORK" && tar czf "$OUTNAME.tgz" "$OUTNAME" && sha256sum "$OUTNAME.tgz" > "$OUTNAME.tgz.sha256" )
ls -l "$WORK/$OUTNAME.tgz"; cat "$WORK/$OUTNAME.tgz.sha256"
echo "Carry $OUTNAME.tgz and $OUTNAME.tgz.sha256 to the airgapped host."
