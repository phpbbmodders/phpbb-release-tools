#!/usr/bin/bash
# php-lint.sh
#
# Lints every PHP file of an extension (or any folder) with `php -l` under
# one or more PHP versions, each in a throwaway Docker container from the
# official php:VERSION-cli image. This checks an extension against its
# minimum PHP version (7.4 for phpbbmodders extensions) without that PHP
# being installed on this machine.
#
# The folder is mounted read-only, the container has no network and is
# removed when it exits. vendor/, node_modules/ and .git/ are skipped.
# Compile-time deprecation notices (for example implicitly nullable
# parameters on PHP 8.4) are shown, and fail the run only with --strict.
#
# Usage:   php-lint.sh [-p VERSIONS] [-s] DIR
# Outputs: one line per PHP version with the file count and result, plus the
#          message for every file that fails, on stdout
# Exit status: 0 when every file passes on every version, 1 when a file
#              fails or Docker fails, 2 for bad arguments.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: php-lint.sh [options] DIR

Lint every PHP file in DIR with `php -l` under each chosen PHP version, in a
throwaway Docker container (official php:VERSION-cli image, DIR mounted
read-only, no network). vendor/, node_modules/ and .git/ are skipped.

Arguments:
  DIR                    the extension's checkout, or any folder of PHP files

Options:
  -p, --php VERSIONS     comma-separated PHP versions (default: 7.4,8.4)
  -s, --strict           also fail on compile-time deprecation notices
  -h, --help             show this help and exit

Examples:
  php-lint.sh ~/repos/contactadmin
  php-lint.sh -p 7.4 ~/repos/contactadmin
  php-lint.sh -p 7.4,8.2,8.4 --strict ~/repos/phpbb-ext-wiki
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

versions="7.4,8.4"
strict=0
dir=""

if [ "$#" -eq 0 ]; then
  usage
  exit 0
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    -p|--php)
      [ "$#" -ge 2 ] || usage_error "$1 needs a value"
      versions="$2"
      shift 2
      ;;
    -s|--strict)
      strict=1
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
      [ -z "$dir" ] || usage_error "only one DIR may be given"
      dir="$1"
      shift
      ;;
  esac
done

[ -n "$dir" ] || usage_error "DIR is required"
[ -d "$dir" ] || usage_error "not a directory: $dir"
dir="$(cd "$dir" && pwd)"

# Validate every version before starting a container: MAJOR.MINOR only, so
# the image tag can't be anything but an official php:X.Y-cli image.
IFS=',' read -r -a version_list <<<"$versions"
[ "${#version_list[@]}" -gt 0 ] || usage_error "--php needs at least one version"
for version in "${version_list[@]}"; do
  [[ "$version" =~ ^[0-9]+\.[0-9]+$ ]] || usage_error "bad PHP version: '$version' (expected MAJOR.MINOR, e.g. 7.4)"
done

command -v docker >/dev/null 2>&1 || die "docker is required but not installed"
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon (is it running, and may this user use it?)"

# Runs inside the container: lints each file, prints "FAIL <file>" with the
# message for a parse error, "DEPRECATED <file>" with the notice for a
# compile-time deprecation, then a "TOTAL <files> <failed> <deprecated>" line.
# shellcheck disable=SC2016 # expanded by the container's shell, not this one
lint_script='
total=0; failed=0; deprecated=0
while IFS= read -r -d "" file; do
  total=$((total + 1))
  if ! out="$(php -d display_errors=stderr -d error_reporting=-1 -l "$file" 2>&1)"; then
    failed=$((failed + 1))
    printf "FAIL %s\n%s\n" "${file#/src/}" "$out"
  elif printf "%s" "$out" | grep -q "Deprecated:"; then
    deprecated=$((deprecated + 1))
    printf "DEPRECATED %s\n%s\n" "${file#/src/}" "$(printf "%s" "$out" | grep "Deprecated:")"
  fi
done < <(find /src \( -name vendor -o -name node_modules -o -name .git \) -prune -o -type f -name "*.php" -print0 | sort -z)
printf "TOTAL %s %s %s\n" "$total" "$failed" "$deprecated"
'

status=0
for version in "${version_list[@]}"; do
  image="php:${version}-cli"
  if ! output="$(docker run --rm --network none --read-only \
      -v "$dir:/src:ro" "$image" bash -c "$lint_script" 2>&1)"; then
    echo "$output" >&2
    die "the PHP $version container failed (could the image $image not be pulled?)"
  fi

  summary="$(grep '^TOTAL ' <<<"$output" | tail -n 1)" || die "no result from the PHP $version container"
  read -r _ total failed deprecated <<<"$summary"
  grep -v '^TOTAL ' <<<"$output" || true

  result="ok"
  if [ "$failed" -gt 0 ]; then
    result="FAIL ($failed with errors)"
    status=1
  elif [ "$deprecated" -gt 0 ]; then
    result="ok, $deprecated with deprecation notices"
    if [ "$strict" -eq 1 ]; then
      result="FAIL ($deprecated with deprecation notices, --strict)"
      status=1
    fi
  fi
  echo "PHP $version: $total files, $result"
done

exit "$status"
