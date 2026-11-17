#!/bin/bash
# Upgrade a locally modified phpIPAM 1.8.1 or 1.8.2 install to 1.9.0 using
# only the bundle this script ships in.
#
#   ./apply-upgrade-1.9.sh /var/www/phpipam /var/www/phpipam-1.9.0
#
# The live install is never written to. A complete 1.9.0 tree is built in the
# new directory with local changes carried across. Rollback: point the web
# server back at the old directory and restore the DB dump.
#
# 1.9.0 moved web files under public/ and reformatted the codebase, so your
# file and the stock 1.8.x file are first normalised with upstream's Rector +
# Pint rules, then 3-way merged onto 1.9.0 ignoring whitespace.
#
# Needs: bash, git, php-cli >= 8.2.

set -uo pipefail

BUNDLE=$(dirname "$(readlink -f "$0")")
NEW=$BUNDLE/phpipam-1.9.0
TOOLS=$BUNDLE/tools
MARKER=.upgrade-1.9            # report dir inside the new tree, OUTSIDE public/

die()  { echo "ERROR: $*" >&2; exit 2; }
note() { echo "$*" | tee -a "$REPORT"; }

[ $# -eq 2 ] || die "usage: $0 <live install> <new directory>"
LIVE=$(readlink -f "$1") || die "cannot resolve $1"
NEWDIR=$(readlink -m "$2")

# ---------------------------------------------------------------- preflight
[ -f "$LIVE/functions/version.php" ] || die "$LIVE does not look like phpIPAM (no functions/version.php)"
[ -e "$NEWDIR" ] && die "$NEWDIR already exists - give a directory that does not exist yet"
case "$NEWDIR/" in "$LIVE"/*) die "the new directory must not be inside the live install" ;; esac
# The bundle is located relative to this script.
if [ ! -d "$NEW" ] && [ ! -d "$BUNDLE/base" ]; then
    die "no bundle next to this script ($BUNDLE).
       Run the copy INSIDE the extracted bundle, not the one in the git repo:
         tar xzf phpipam-upgrade-1.9.0.tgz && cd phpipam-upgrade-1.9.0 && ./apply-upgrade-1.9.sh ...
       (Build the bundle with make-bundle-1.9.sh, or fetch it from the package registry.)"
fi
[ -f "$NEW/vendor/autoload.php" ] || die "bundle incomplete: $NEW/vendor/autoload.php missing - the tarball was only partly extracted or copied. Re-check it with: sha256sum -c phpipam-upgrade-1.9.0.tgz.sha256"
[ -x "$TOOLS/vendor/bin/rector" ] && [ -x "$TOOLS/vendor/bin/pint" ] && [ -x "$TOOLS/rector-2.1/vendor/bin/rector" ] || die "bundle incomplete: Rector/Pint missing under $TOOLS"
command -v git >/dev/null || die "git is required for the merge (dnf install git-core)"
command -v php >/dev/null || die "php-cli is required"
php -r 'exit(PHP_VERSION_ID >= 80200 ? 0 : 1);' || die "phpIPAM 1.9.0 needs PHP >= 8.2; php-cli here is $(php -r 'echo PHP_VERSION;')"
# Pint will not start without these.
for e in mbstring xml tokenizer; do
    php -r "exit(extension_loaded('$e') ? 0 : 1);" || die "php-cli lacks the '$e' extension, needed to normalise code before merging (dnf install php-mbstring php-xml, or php8.4-mbstring php8.4-xml on the 8.4 stream)"
done

missing_ext=()
for e in ctype curl dom filter gd gettext gmp iconv json libxml mbstring openssl pcre pdo pdo_mysql session sockets; do
    php -r "exit(extension_loaded('$e') ? 0 : 1);" || missing_ext+=("$e")
done

v=$(sed -n "s/.*VERSION_VISIBLE['\"] *, *['\"]\([0-9.]*\)['\"].*/\1/p" "$LIVE/functions/version.php" | head -1)
case "$v" in
    1.8.1|1.8.2) ;;
    *) die "live install reports version '$v'; this bundle upgrades from 1.8.1 or 1.8.2 only" ;;
esac
BASE=$BUNDLE/base/$v
[ -d "$BASE" ] || die "bundle incomplete: $BASE missing"

# 1.8.x path -> 1.9.0 path (verified by make-bundle-1.9.sh).
map() {
    case "$1" in
        api/controllers/*) echo "functions/$1" ;;
        api/*|app/*|css/*|js/*|install/*|upgrade/*|doc/*|index.php|robots.txt|.htaccess) echo "public/$1" ;;
        *) echo "$1" ;;
    esac
}
# Never carried: secrets/state handled separately, VCS metadata, backups, editor/patch leftovers.
skip() {
    case "$1" in
        config.php|db/bkp/*|*.orig|*.rej|*.swp|*.swo|*~) return 0 ;;
    esac
    return 1
}

# Same content? Symlinks are compared by target, never followed.
same() {
    if [ -L "$1" ] || [ -L "$2" ]; then
        [ -L "$1" ] && [ -L "$2" ] && [ "$(readlink "$1")" = "$(readlink "$2")" ]
    else
        cmp -s "$1" "$2"
    fi
}

# ---------------------------------------------------------------- build tree
mkdir -p "$(dirname "$NEWDIR")" || die "cannot create parent of $NEWDIR"
cp -a "$NEW" "$NEWDIR" || die "copy of 1.9.0 into $NEWDIR failed"
mkdir -p "$NEWDIR/$MARKER"/{yours,collisions,orphans}
REPORT=$NEWDIR/$MARKER/report.txt
: > "$REPORT"

note "phpIPAM $v -> 1.9.0"
note "  live (read only) : $LIVE"
note "  new tree         : $NEWDIR"
note "  php-cli          : $(php -r 'echo PHP_VERSION;')"
[ ${#missing_ext[@]} -gt 0 ] && note "  WARNING: php-cli lacks: ${missing_ext[*]} (check php-fpm too: 1.9.0 needs them)"
note ""

# ---------------------------------------------------------------- classify
modified=(); added=(); deleted=()
while IFS= read -r f; do
    skip "$f" && continue
    if [ -e "$BASE/$f" ] || [ -L "$BASE/$f" ]; then
        same "$LIVE/$f" "$BASE/$f" || modified+=("$f")
    else
        added+=("$f")
    fi
done < <(cd "$LIVE" && find . -name .git -prune -o \( -type f -o -type l \) -printf '%P\n' | sort)

while IFS= read -r f; do
    skip "$f" && continue
    [ -e "$LIVE/$f" ] || [ -L "$LIVE/$f" ] || deleted+=("$f")
done < <(cd "$BASE" && find . \( -type f -o -type l \) -printf '%P\n' | sort)

note "Local changes found against stock $v:"
note "  modified : ${#modified[@]}"
note "  added    : ${#added[@]}"
note "  deleted  : ${#deleted[@]}"
note ""

# Record local changes (outside the web root).
for f in "${modified[@]}"; do
    [ -L "$LIVE/$f" ] || [ -L "$BASE/$f" ] && { echo "symlink $f: stock -> $(readlink "$BASE/$f"), yours -> $(readlink "$LIVE/$f")" >> "$NEWDIR/$MARKER/your-changes.diff"; continue; }
    diff -u --label "stock-$v/$f" --label "yours/$f" "$BASE/$f" "$LIVE/$f" >> "$NEWDIR/$MARKER/your-changes.diff"
    mkdir -p "$NEWDIR/$MARKER/yours/$(dirname "$f")"; cp -p "$LIVE/$f" "$NEWDIR/$MARKER/yours/$f"
done

# ---------------------------------------------------------------- carry
n_carry=0 n_add=0 n_orphan=0 n_collide=0
to_merge=(); touched=()

for f in "${modified[@]}"; do
    g=$(map "$f")
    if [ -L "$LIVE/$f" ] || [ -L "$BASE/$f" ] || [ -L "$NEW/$g" ]; then
        # A symlink on any side: carry yours as-is (re-pointed links are not mergeable).
        mkdir -p "$NEWDIR/$(dirname "$g")"; rm -f "$NEWDIR/$g"; cp -P "$LIVE/$f" "$NEWDIR/$g"; n_carry=$((n_carry+1))
        note "CARRIED  $f -> $g   (symlink -> $(readlink "$LIVE/$f" 2>/dev/null || echo 'regular file'))"
    elif [ ! -e "$NEW/$g" ]; then
        n_orphan=$((n_orphan+1))
        mkdir -p "$NEWDIR/$MARKER/orphans/$(dirname "$f")"; cp -p "$LIVE/$f" "$NEWDIR/$MARKER/orphans/$f"
        note "ORPHAN   $f   (1.9.0 no longer ships this file - your edit is NOT applied; copy in $MARKER/orphans/)"
    elif same "$BASE/$f" "$NEW/$g"; then
        # Upstream did not touch this file: your version is exact.
        cp -p "$LIVE/$f" "$NEWDIR/$g"; touched+=("$g"); n_carry=$((n_carry+1))
        note "CARRIED  $f -> $g"
    else
        to_merge+=("$f")
    fi
done

for f in "${added[@]}"; do
    g=$(map "$f")
    if [ -e "$NEW/$g" ]; then
        n_collide=$((n_collide+1))
        mkdir -p "$NEWDIR/$MARKER/collisions/$(dirname "$f")"; cp -p "$LIVE/$f" "$NEWDIR/$MARKER/collisions/$f"
        note "COLLIDES $f -> $g   (1.9.0 now ships a file there; kept 1.9.0's, yours in $MARKER/collisions/)"
    else
        mkdir -p "$NEWDIR/$(dirname "$g")"; cp -pP "$LIVE/$f" "$NEWDIR/$g"; touched+=("$g"); n_add=$((n_add+1))
        note "ADDED    $f -> $g"
        case "$f" in api/controllers/custom/*)
            note "         ^ custom API controller: check it against 1.9.0's functions/api/controllers/Common.php" ;;
        esac
    fi
done

# ---------------------------------------------------------------- normalise
# Normalise the stock and local copies with upstream's rules so the merge
# sees only real changes. Each side is a scratch tree laid out like 1.9.0 so
# rector.php paths and pint.json excludes apply as upstream.
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/phpipam-upg.XXXXXX")
trap 'rm -rf "$SCRATCH"' EXIT
normalised=0
php_merges=()
for f in "${to_merge[@]}"; do case "$f" in *.php) php_merges+=("$f") ;; esac; done

# Tool failure is fatal: un-normalised input conflicts on nearly every file.
runtool() { # side label cmd...
    local side=$1 label=$2; shift 2
    ( cd "$SCRATCH/$side" && "$@" ) >"$SCRATCH/$side.$label.log" 2>&1
    local rc=$?
    if [ $rc -ne 0 ]; then
        tail -25 "$SCRATCH/$side.$label.log" >&2
        rm -rf "$NEWDIR"
        die "$label failed (rc=$rc) normalising the $side side - output above. $NEWDIR removed; fix and re-run."
    fi
}

if [ ${#php_merges[@]} -gt 0 ]; then
    for side in base yours; do
        mkdir -p "$SCRATCH/$side/functions" "$SCRATCH/$side/public"
        cp "$TOOLS/rector-normalise.php" "$SCRATCH/$side/rector.php"
        cp "$TOOLS/rector-casts.php" "$SCRATCH/$side/rector-casts.php"
        cp "$NEW/pint.json" "$SCRATCH/$side/pint.json"
        for f in "${php_merges[@]}"; do
            g=$(map "$f"); src=$BASE/$f; [ $side = yours ] && src=$LIVE/$f
            mkdir -p "$SCRATCH/$side/$(dirname "$g")"; cp "$src" "$SCRATCH/$side/$g"
            # 1.9.0 replaced the pf_explode() polyfill with explode().
            sed -i -E 's/\bpf_explode[[:space:]]*\(/explode(/g' "$SCRATCH/$side/$g"
        done
        cp -a "$SCRATCH/$side" "$SCRATCH/$side.pre-rector"
        # 1.9.0's (string) casts need Rector 2.1.0's NullToStrictStringFuncCallArgRector.
        runtool $side rector-casts php "$TOOLS/rector-2.1/vendor/bin/rector" process --no-progress-bar --no-diffs --config rector-casts.php
        runtool $side rector php "$TOOLS/vendor/bin/rector" process --no-progress-bar --no-diffs --config rector.php
    done
    # Rector can emit unparseable code for mixed HTML/PHP. If either side fails
    # to parse, use the pre-Rector copy on both sides.
    for f in "${php_merges[@]}"; do
        g=$(map "$f")
        if ! php -l "$SCRATCH/base/$g" >/dev/null 2>&1 || ! php -l "$SCRATCH/yours/$g" >/dev/null 2>&1; then
            cp "$SCRATCH/base.pre-rector/$g" "$SCRATCH/base/$g"
            cp "$SCRATCH/yours.pre-rector/$g" "$SCRATCH/yours/$g"
            note "NOTE     $g: Rector output did not parse - normalised with Pint only (more conflicts likely)"
        fi
    done
    for side in base yours; do
        runtool $side pint php "$TOOLS/vendor/bin/pint" --config pint.json .
    done
    normalised=1
fi

# ---------------------------------------------------------------- merge
# Whitespace-insensitive merge needs `git merge -Xignore-all-space` (merge-file
# and diff3 lack it): throwaway repo, isolated from system/user git config.
wsmerge() { # ours base theirs out
    local d; d=$(mktemp -d "$SCRATCH/m.XXXXXX")
    (
        export HOME=$d GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
               GIT_AUTHOR_NAME=phpipam-upgrade GIT_AUTHOR_EMAIL=upgrade@localhost \
               GIT_COMMITTER_NAME=phpipam-upgrade GIT_COMMITTER_EMAIL=upgrade@localhost
        cd "$d" || exit 3
        git init -q -b base . && git config core.autocrlf false &&
        cp "$2" f && git add f && git commit -qm base &&
        git checkout -qb phpipam-1.9.0 && cp "$3" f && git commit -q --allow-empty -am new &&
        git checkout -q base && git checkout -qb yours && cp "$1" f && git commit -q --allow-empty -am yours || exit 3
        git merge -q --no-edit -Xignore-all-space phpipam-1.9.0 >/dev/null 2>&1
    )
    local rc=$?
    cp "$d/f" "$4"
    return $rc
}

n_merged=0 n_conflict=0 n_binary=0
conflicts=()
for f in "${to_merge[@]}"; do
    g=$(map "$f")
    ours=$LIVE/$f; base=$BASE/$f
    if [ $normalised -eq 1 ] && [[ $f == *.php ]]; then
        [ -s "$SCRATCH/yours/$g" ] && ours=$SCRATCH/yours/$g
        [ -s "$SCRATCH/base/$g" ]  && base=$SCRATCH/base/$g
    fi
    out=$SCRATCH/out; rm -f "$out"
    wsmerge "$ours" "$base" "$NEW/$g" "$out"
    rc=$?
    if [ $rc -eq 0 ]; then
        cat "$out" > "$NEWDIR/$g"; touched+=("$g"); n_merged=$((n_merged+1))
        note "MERGED   $f -> $g"
    elif [ $rc -eq 3 ]; then
        die "internal merge setup failed for $f (is git >= 2.28?)"
    elif grep -q '^<<<<<<< ' "$out"; then
        cat "$out" > "$NEWDIR/$g"; touched+=("$g"); n_conflict=$((n_conflict+1)); conflicts+=("$g")
        note "CONFLICT $f -> $g   <-- resolve by hand"
    else
        # Binary (e.g. a .mo) - no markers possible. Keep 1.9.0's; yours is in yours/.
        n_binary=$((n_binary+1)); conflicts+=("$g (binary: 1.9.0 kept, yours in $MARKER/yours/$f)")
        note "CONFLICT $f -> $g   (binary - 1.9.0 version kept, yours in $MARKER/yours/)"
    fi
done

# ---------------------------------------------------------------- config.php
if [ -f "$LIVE/config.php" ]; then
    cp -p "$LIVE/config.php" "$NEWDIR/config.php"; touched+=("config.php")
    note "CONFIG   config.php copied unchanged (1.9.0's config.dist.php only changed formatting)"

    # 1.9.0 sets BASE from DOCUMENT_ROOT before reading config.php, so a
    # sub-path install gets BASE='/' and every asset 404s. If config.php sets
    # a non-root BASE, pin it in functions.php.
    cfg_base=$(sed -n "s/^[[:space:]]*define[[:space:]]*([[:space:]]*['\"]BASE['\"][[:space:]]*,[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "$LIVE/config.php" | tail -1)
    if [ -n "$cfg_base" ] && [ "$cfg_base" != "/" ]; then
        fx=$NEWDIR/functions/functions.php
        anchor='// auto-set base if not already defined'
        if grep -qF "$anchor" "$fx" && ! grep -q "LOCAL: BASE pinned" "$fx"; then
            awk -v b="$cfg_base" -v a="$anchor" '
                $0 == a {
                    print "// LOCAL: BASE pinned by apply-upgrade-1.9.sh from config.php. phpIPAM 1.9.0"
                    print "// auto-detects BASE from DOCUMENT_ROOT before config.php is read, which"
                    print "// under a sub-path (nginx alias) gives \x27/\x27 and breaks every asset URL."
                    print "if (! defined(\x27BASE\x27)) {"
                    print "    define(\x27BASE\x27, \x27" b "\x27);"
                    print "}"
                    print ""
                }
                { print }' "$fx" > "$fx.tmp" && cat "$fx.tmp" > "$fx" && rm -f "$fx.tmp"
            touched+=("functions/functions.php")
            note "BASE     config.php sets BASE='$cfg_base' - pinned in functions/functions.php (1.9.0 ignores it in config.php)"
        elif ! grep -q "LOCAL: BASE pinned" "$fx"; then
            note "WARNING: config.php sets BASE='$cfg_base' but functions.php changed shape - pin it by hand or every asset URL breaks"
        fi
    fi
else
    note "WARNING: no config.php in $LIVE - copy it in before use"
fi

for f in "${deleted[@]}"; do
    note "DELETED  $f   (you had removed this; 1.9.0 ships it at $(map "$f") - remove it again if intended)"
done

# ---------------------------------------------------------------- ownership
# As root: copy the live tree's ownership, including per-directory exceptions.
if [ "$(id -u)" -eq 0 ]; then
    chown -R --reference="$LIVE" "$NEWDIR"
    while IFS= read -r d; do
        nd=$NEWDIR/$(map "$d")
        [ -d "$nd" ] && chown --reference="$LIVE/$d" "$nd" && note "OWNER    $(map "$d") <- $(stat -c %U:%G "$LIVE/$d") (as in live)"
    done < <(cd "$LIVE" && find . -name .git -prune -o -type d \! -user "$(stat -c %u "$LIVE")" -printf '%P\n')
    chown -R root:root "$NEWDIR/$MARKER"; chmod 700 "$NEWDIR/$MARKER"
fi

# ---------------------------------------------------------------- checks
note ""
bad=0
for g in "${touched[@]}"; do
    [[ $g == *.php ]] || continue
    if ! out=$(php -l "$NEWDIR/$g" 2>&1); then
        bad=$((bad+1)); note "SYNTAX   $g: $(echo "$out" | grep -m1 -i error)"
    fi
done
# php -l misses calls to the removed pf_explode().
for g in "${touched[@]}"; do
    [[ $g == *.php ]] || continue
    if grep -qE '\bpf_explode[[:space:]]*\(' "$NEWDIR/$g"; then
        bad=$((bad+1))
        note "RUNTIME  $g calls pf_explode(), removed in 1.9.0 - use explode(\$sep, (string) \$value)"
    fi
done
# php -l also misses includes of files 1.9.0 removed.
gone='php_poly_fill\.php|include-only\.php|class\.Radius\.php|functions/vendor/|(LdapRecord|php-saml|PHPMailer|parsedown|xmlseclibs|GoogleAuthenticator|qrcodejs)/'
for g in "${touched[@]}"; do
    [[ $g == *.php ]] || continue
    if hits=$(grep -nE "(require|include)(_once)?[^;]*($gone)" "$NEWDIR/$g"); then
        bad=$((bad+1))
        note "RUNTIME  $g includes a file 1.9.0 removed:"
        echo "$hits" | head -3 | sed 's/^/           line /' | tee -a "$REPORT"
        note "         (php_poly_fill.php only defined pf_explode(); submodules -> vendor/ via composer; LdapRecord is gone, adLDAP remains at functions/adLDAP/src/adLDAP.php)"
    fi
done
mapfile -t markers < <(cd "$NEWDIR" && grep -rl --exclude-dir="$MARKER" --exclude-dir=vendor '^<<<<<<< ' . 2>/dev/null)

note "================ SUMMARY ================"
note "  carried as-is (upstream unchanged) : $n_carry"
note "  merged cleanly                     : $n_merged"
note "  CONFLICTS                          : $((n_conflict+n_binary))"
note "  added files carried                : $n_add"
note "  orphaned (file gone in 1.9.0)      : $n_orphan"
note "  collisions (name now used)         : $n_collide"
note "  locally deleted                    : ${#deleted[@]}"
note "  php -l / pf_explode failures       : $bad"
if [ ${#conflicts[@]} -gt 0 ]; then
    note ""
    note "RESOLVE THESE ('<<<<<<< HEAD' = yours, '>>>>>>> phpipam-1.9.0' = upstream;"
    note "keep the upstream side wherever you can - 1.9.0 is a security release):"
    for c in "${conflicts[@]}"; do note "  $NEWDIR/$c"; done
fi
[ ${#markers[@]} -gt 0 ] && note "" && note "Files still containing conflict markers: ${markers[*]}"

cat <<EOF | tee -a "$REPORT"

Your original changes, for reference: $NEWDIR/$MARKER/your-changes.diff
Full report: $REPORT          (outside public/, not web-served)

NEXT STEPS
 1. Resolve any conflicts above, then:  php -l <file>
 2. Database (back it up first). Check you are on dbversion 46:
      mysql -e "SELECT version, dbversion FROM settings" <db>
    then generate ONLY the 1.9 queries - note the argument is 1.8.46,
    NOT 1.8 (which replays 1.8.43-1.8.46 against a DB that already has them):
      php $NEWDIR/functions/upgrade_queries.php 1.8.46 > /root/phpipam-1.9.sql
      mysql <db> < /root/phpipam-1.9.sql
 3. nginx: root must now be $NEWDIR/public  (see configs/nginx-phpipam-1.9.conf)
 4. SELinux: restorecon -RFv $NEWDIR   and re-create any semanage fcontext
    rules you had for $LIVE against the new path:
      semanage fcontext -l -C | grep '$LIVE'
 5. Cron jobs calling $LIVE/functions/scripts/*.php -> $NEWDIR/functions/scripts/
 6. systemctl reload nginx php-fpm, log in, check Administration -> Version check.
Rollback: point nginx back at the old root and reload the DB dump.
EOF

[ $((n_conflict+n_binary+bad)) -gt 0 ] && exit 1
exit 0
