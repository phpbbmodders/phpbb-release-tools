#!/usr/bin/bash
# run-epv.sh
#
# Runs EPV (phpBB's Extension Pre Validator) on an extension the way the
# phpbbmodders reusable workflow (phpbbmodders/.github, epv.yml) does in CI:
# EPV is pointed at phpBB/ext/ of a phpBB checkout, and the run fails on any
# Fatal or Error finding the extension does not list in
# .github/epv-allowed.txt. EPV itself exits 0 on Error findings, so running it
# by hand misses what fails CI.
#
# The first run sets up the checkout: a shallow clone of phpBB at the chosen
# branch with EPV installed through Composer. Every run copies the extension
# in afresh (rsync --delete, without .git) as the only extension in
# phpBB/ext/.
#
# Usage:   run-epv.sh [-b BRANCH] [-e DIR] [-u] EXT_DIR
# Outputs: EPV's report, then the findings that would fail CI, on stdout
# Exit status: 0 when no finding outside the allowed list, 1 when there is one
#              or setup fails, 2 for bad arguments.

set -Eeuo pipefail

readonly PHPBB_REPO="https://github.com/phpbb/phpbb.git"

usage() {
  cat <<'EOF'
Usage: run-epv.sh [options] EXT_DIR

Run EPV on a phpBB extension and fail on the Fatal or Error findings that
the phpbbmodders CI fails on: every one not listed in the extension's
.github/epv-allowed.txt. The phpBB checkout with EPV is set up on first use;
the extension is copied in afresh on every run.

Arguments:
  EXT_DIR                the extension's checkout (with composer.json)

Options:
  -b, --branch BRANCH    phpBB branch to check against: 3.3.x or master
                         (default: 3.3.x)
  -e, --env DIR          checkout to use or create (default: $PHPBB_EPV_ENV,
                         else ~/.cache/phpbb-release-tools/epv-BRANCH)
  -u, --update           pull the checkout and reinstall EPV first
  -h, --help             show this help and exit

Examples:
  run-epv.sh ~/repos/adduser
  run-epv.sh -b master ~/repos/adduser
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

usage_error() {
  echo "Error: $*" >&2
  echo "Run with --help for usage." >&2
  exit 2
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"
}

branch="3.3.x"
env_dir=""
update=0
ext_dir=""

if [ "$#" -eq 0 ]; then
  usage
  exit 0
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    -b|--branch)
      [ "$#" -ge 2 ] || usage_error "$1 needs a branch"
      branch="$2"
      shift 2
      ;;
    -e|--env)
      [ "$#" -ge 2 ] || usage_error "$1 needs a directory"
      env_dir="$2"
      shift 2
      ;;
    -u|--update)
      update=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      usage_error "unknown option: $1"
      ;;
    *)
      [ -z "$ext_dir" ] || usage_error "only one EXT_DIR can be given"
      ext_dir="$1"
      shift
      ;;
  esac
done

[ -n "$ext_dir" ] || usage_error "EXT_DIR is required"
case "$branch" in
  3.3.x|master) ;;
  *) usage_error "--branch must be 3.3.x or master, not $branch" ;;
esac
[ -d "$ext_dir" ] || usage_error "$ext_dir is not a directory"
ext_dir="$(cd -- "$ext_dir" && pwd)"
[ -f "$ext_dir/composer.json" ] || usage_error "$ext_dir has no composer.json"

for tool in git php rsync jq; do
  need "$tool"
done

ext_name="$(jq -r '.name // empty' "$ext_dir/composer.json")"
[[ "$ext_name" =~ ^[a-z0-9_-]+/[a-z0-9_-]+$ ]] || die "composer.json has no valid vendor/name: '$ext_name'"

env_dir="${env_dir:-${PHPBB_EPV_ENV:-$HOME/.cache/phpbb-release-tools/epv-$branch}}"

# As the CI workflow does, with phpBB's own composer.phar (a distro Composer
# may be too old for these settings): EPV's dev-master cannot install next to phpBB 4.0's
# Symfony 7, and Composer 2.9 otherwise refuses some packages phpBB 3.3 locks.
install_epv() {
  local epv_branch="dev-master"
  [ "$branch" = "master" ] && epv_branch="dev-epv-phpbb4"
  echo "Installing EPV ($epv_branch) in $env_dir/phpBB ..." >&2
  (
    cd "$env_dir/phpBB"
    php ../composer.phar config audit.block-insecure false
    php ../composer.phar require "phpbb/epv:$epv_branch" --dev --no-interaction --no-progress --ignore-platform-reqs
  ) >&2
}

if [ ! -d "$env_dir/.git" ]; then
  [ ! -e "$env_dir" ] || [ -z "$(ls -A -- "$env_dir")" ] || die "$env_dir exists and is not a phpBB checkout"
  echo "Cloning phpBB $branch into $env_dir ..." >&2
  mkdir -p -- "$(dirname -- "$env_dir")"
  git clone --quiet --depth 1 --branch "$branch" "$PHPBB_REPO" "$env_dir" >&2
  install_epv
elif [ "$update" -eq 1 ]; then
  echo "Updating $env_dir ..." >&2
  git -C "$env_dir" checkout --quiet -- . >&2
  git -C "$env_dir" pull --quiet --ff-only >&2
  install_epv
fi

[ -f "$env_dir/phpBB/vendor/bin/EPV.php" ] || die "$env_dir has no phpBB/vendor/bin/EPV.php; run again with --update"

# The extension is the only one under phpBB/ext/, copied in afresh, so EPV
# reports on nothing else and never on an old copy.
find "$env_dir/phpBB/ext" -mindepth 1 -maxdepth 1 -type d -exec rm -rf -- {} +
target="$env_dir/phpBB/ext/$ext_name"
mkdir -p -- "$target"
rsync -a --delete --exclude .git --exclude node_modules "$ext_dir/" "$target/"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

cd -- "$env_dir"
php phpBB/vendor/bin/EPV.php run --dir="phpBB/ext/" | sed 's/\x1b\[[0-9;]*m//g' | tee "$work/epv.txt"

# Fatal and Error lines, without line numbers or the vendor/name prefix, as
# the allowed list writes them
{ grep -E '^(Fatal|Error):' "$work/epv.txt" || true; } \
  | sed -E "s# on line [0-9]+##; s#/$ext_name/#/#g" | sort -u > "$work/findings.txt"
allowed="$ext_dir/.github/epv-allowed.txt"
if [ -f "$allowed" ]; then
  { grep -vE '^\s*(#|$)' "$allowed" || true; } | sort -u > "$work/allowed.txt"
else
  : > "$work/allowed.txt"
fi
comm -23 "$work/findings.txt" "$work/allowed.txt" > "$work/blocking.txt"
comm -13 "$work/findings.txt" "$work/allowed.txt" > "$work/stale.txt"

echo
if [ -s "$work/stale.txt" ]; then
  echo "Allowed findings that no longer occur; remove them from .github/epv-allowed.txt:"
  cat "$work/stale.txt"
fi
if [ -s "$work/blocking.txt" ]; then
  echo "EPV findings that would fail CI (not in .github/epv-allowed.txt):"
  cat "$work/blocking.txt"
  exit 1
fi
echo "No EPV Fatal or Error findings outside .github/epv-allowed.txt."
