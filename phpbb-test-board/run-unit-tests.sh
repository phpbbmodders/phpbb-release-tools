#!/usr/bin/bash
# run-unit-tests.sh
#
# Runs a phpBB extension's PHPUnit tests the way phpbb-extensions/test-framework
# does in CI: inside a phpBB checkout, with phpBB's own PHPUnit and test
# bootstrap.
#
# The first run sets that checkout up: a shallow clone of phpBB at the chosen
# branch, its Composer dependencies, and, on PHP 8 with phpBB 3.3.x, the switch
# to PHPUnit 9 that phpBB's .github/setup-phpbb.sh makes (3.3.x ships PHPUnit
# 7.5, which does not run on PHP 8.1 and later). Every run copies the
# extension in afresh (rsync --delete, without .git), so the tests never run
# against an old copy, then runs its phpunit.xml.dist.
#
# Usage:   run-unit-tests.sh [-b BRANCH] [-e DIR] [-u] [-f PATTERN] EXT_DIR [-- PHPUNIT_ARGS...]
# Outputs: PHPUnit's report on stdout
# Exit status: PHPUnit's (0 when every test passes), 1 for a setup failure,
#              2 for bad arguments.

set -Eeuo pipefail

readonly PHPBB_REPO="https://github.com/phpbb/phpbb.git"

usage() {
  cat <<'EOF'
Usage: run-unit-tests.sh [options] EXT_DIR [-- PHPUNIT_ARGS...]

Run a phpBB extension's PHPUnit tests inside a phpBB checkout, as the
phpbb-extensions test framework does in CI. The checkout is set up on first
use; the extension is copied in afresh on every run.

Arguments:
  EXT_DIR                the extension's checkout (with composer.json,
                         phpunit.xml.dist and a tests/functional folder,
                         which may be empty)
  PHPUNIT_ARGS           anything after -- is passed to PHPUnit

Options:
  -b, --branch BRANCH    phpBB branch to test against: 3.3.x or master
                         (default: 3.3.x)
  -e, --env DIR          phpBB checkout to use or create (default:
                         $PHPBB_UNIT_ENV, else
                         ~/.cache/phpbb-release-tools/phpbb-BRANCH)
  -u, --update           pull the checkout and reinstall its dependencies first
  -f, --filter PATTERN   run only the tests matching PATTERN (PHPUnit --filter)
  -h, --help             show this help and exit

Examples:
  run-unit-tests.sh ~/repos/phpbb-ext-wiki
  run-unit-tests.sh -b master ~/repos/phpbb-ext-wiki
  run-unit-tests.sh -f readable_diff ~/repos/phpbb-ext-wiki -- --testdox
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
filter=""
ext_dir=""
phpunit_args=()

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
    -f|--filter)
      [ "$#" -ge 2 ] || usage_error "$1 needs a pattern"
      filter="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      phpunit_args=("$@")
      break
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
[ -f "$ext_dir/phpunit.xml.dist" ] || usage_error "$ext_dir has no phpunit.xml.dist"
# The test framework's CI runs with phpBB's own configuration, which lists the
# extension's tests/functional folder; its PostgreSQL, MySQL, MSSQL and Windows
# jobs stop when the folder is missing, even with functional tests turned off
[ -d "$ext_dir/tests/functional" ] ||
  usage_error "$ext_dir has no tests/functional folder, which the test framework's CI jobs need even when empty: mkdir -p tests/functional && touch tests/functional/.gitkeep"

for tool in git php composer rsync jq; do
  need "$tool"
done

# The extension's place under phpBB/ext/ comes from its Composer name.
ext_name="$(jq -r '.name // empty' "$ext_dir/composer.json")"
[[ "$ext_name" =~ ^[a-z0-9_-]+/[a-z0-9_-]+$ ]] || die "composer.json has no valid vendor/name: '$ext_name'"

env_dir="${env_dir:-${PHPBB_UNIT_ENV:-$HOME/.cache/phpbb-release-tools/phpbb-$branch}}"
php_major="$(php -r 'echo PHP_MAJOR_VERSION;')"

# phpBB 3.3.x needs PHPUnit 9 on PHP 8, as phpBB's own CI installs it.
use_phpunit9() {
  (
    cd "$env_dir/phpBB"
    composer remove phpunit/dbunit --dev --update-with-dependencies --no-interaction
    composer require symfony/yaml:^4.4 misantron/dbunit:5.2.1 phpunit/phpunit:^9.3 doctrine/instantiator:^1.4 \
      --dev --update-with-all-dependencies --ignore-platform-reqs --no-interaction
  )
}

install_dependencies() {
  echo "Installing phpBB's Composer dependencies in $env_dir/phpBB ..." >&2
  (cd "$env_dir/phpBB" && composer install --no-interaction --no-progress) >&2
  if [ "$branch" = "3.3.x" ] && [ "$php_major" -ge 8 ]; then
    echo "Switching to PHPUnit 9 for PHP $php_major ..." >&2
    use_phpunit9 >&2
  fi
}

if [ ! -d "$env_dir/.git" ]; then
  [ ! -e "$env_dir" ] || [ -z "$(ls -A -- "$env_dir")" ] || die "$env_dir exists and is not a phpBB checkout"
  echo "Cloning phpBB $branch into $env_dir ..." >&2
  mkdir -p -- "$(dirname -- "$env_dir")"
  git clone --quiet --depth 1 --branch "$branch" "$PHPBB_REPO" "$env_dir" >&2
  install_dependencies
elif [ "$update" -eq 1 ]; then
  echo "Updating $env_dir ..." >&2
  git -C "$env_dir" checkout --quiet -- . >&2
  git -C "$env_dir" pull --quiet --ff-only >&2
  install_dependencies
fi

[ -x "$env_dir/phpBB/vendor/bin/phpunit" ] || die "$env_dir has no phpBB/vendor/bin/phpunit; run again with --update"
[ -f "$env_dir/tests/bootstrap.php" ] || die "$env_dir has no tests/bootstrap.php"

# A fresh copy every run, so no old file of the extension is left behind.
target="$env_dir/phpBB/ext/$ext_name"
mkdir -p -- "$target"
rsync -a --delete --exclude .git --exclude node_modules "$ext_dir/" "$target/"

args=(--configuration "phpBB/ext/$ext_name/phpunit.xml.dist" --bootstrap ./tests/bootstrap.php)
[ -z "$filter" ] || args+=(--filter "$filter")

cd -- "$env_dir"
exec phpBB/vendor/bin/phpunit "${args[@]}" "${phpunit_args[@]}"
