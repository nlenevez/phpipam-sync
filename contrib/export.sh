#!/bin/sh
# Export one phpIPAM instance into its data repo, for cron. No CI needed.
#
#   tool/contrib/export.sh [ENV_FILE]
#
# Run from the root of the data repo (phpipam-sync.yml, tool/, snapshot/).
# Fast-forwards from the remote, exports into snapshot/, commits only if
# something changed, and pushes. The last line says ok or FAILED.
#
# ENV_FILE (optional, outside the repo, mode 0600) is sourced first:
#   PHPIPAM_SRC_TOKEN=...       the variable named by source.token_env
#   PYTHON=/usr/bin/python3.12  3.9+ (RHEL 8's python3 is 3.6)
#   EXPORT_CONFIG=phpipam-sync.yml
#   EXPORT_DIR=snapshot
#   EXPORT_REMOTE=origin        "none" = commit locally only
#   GIT_SSH_COMMAND="ssh -i ~/.ssh/ipam_export -o BatchMode=yes"
set -eu

if [ $# -ge 1 ]; then
    set -a
    # shellcheck disable=SC1090
    . "$1"
    set +a
fi

config=${EXPORT_CONFIG:-phpipam-sync.yml}
outdir=${EXPORT_DIR:-snapshot}
remote=${EXPORT_REMOTE:-origin}
py=${PYTHON:-python3}

if [ ! -f "$config" ] || [ ! -f tool/ipam_export.py ]; then
    echo "run from the root of a data repo ($config and tool/ expected)" >&2
    exit 1
fi

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
finish() {
    r=$?
    [ $r -eq 0 ] && word=ok || word=FAILED
    echo "$(stamp) result: $word (exit $r)"
}
trap finish EXIT

exec 9>"$(git rev-parse --git-dir)/ipam-export.lock"
if ! flock -n 9; then
    echo "another export is already running here" >&2
    exit 1
fi

echo "$(stamp) export start in $(pwd)"

if ! git config user.name >/dev/null; then
    export GIT_AUTHOR_NAME="phpIPAM export" GIT_COMMITTER_NAME="phpIPAM export"
fi
if ! git config user.email >/dev/null; then
    export GIT_AUTHOR_EMAIL="ipam-export@$(hostname)" GIT_COMMITTER_EMAIL="ipam-export@$(hostname)"
fi

push=""
if [ "$remote" != none ]; then
    branch=$(git symbolic-ref --short HEAD)
    git fetch --quiet "$remote" "$branch"
    git merge --quiet --ff-only "$remote/$branch"
    push="--push"
fi
git submodule update --init --quiet

mkdir -p "$outdir"
"$py" tool/ipam_export.py --config "$config" --out-dir "$outdir" --commit $push
