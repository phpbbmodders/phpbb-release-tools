#!/usr/bin/bash
# Regression tests for git-extensions.sh and git-release-style.sh --create, plus
# option handling of every local release script. GitHub publishing is covered
# by tests/test-release-api.sh.
# Runs offline in a temporary directory.
# Usage: tests/test-release-scripts.sh
# Exit status: 0 if every test passes, 1 otherwise.

set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

failures=0

# Record the result of one test: check "<name>" <command...>
check() {
  local name="$1"
  shift
  if "$@"; then
    echo "ok   - $name"
  else
    echo "FAIL - $name"
    failures=$((failures + 1))
  fi
}

commit() {
  git -c user.name=test -c user.email=test@example.com commit -q "$@"
}

# Create a fresh extension repository in $tmp/work with one commit.
new_repo() {
  rm -rf "$tmp/work" "$tmp/git-exported"
  mkdir -p "$tmp/work"
  cd "$tmp/work" || exit 1
  git init -q -b main
  echo '{"name":"acme/myext","version":"1.2.3"}' >composer.json
  echo hi >a.txt
  git add -A
  commit -m init
}

zip_file="$tmp/git-exported/acme-myext-1.2.3.zip"

zip_is_valid() { unzip -tq "$zip_file" >/dev/null 2>&1; }
zip_has_prefix() { unzip -l "$zip_file" | grep -q ' acme/myext/a.txt$'; }
zip_comment_empty() { [[ -z "$(unzip -z "$zip_file" | tail -n +2)" ]]; }
no_partial_files() { [[ -z "$(find "$tmp/git-exported" -name '*.partial' 2>/dev/null)" ]]; }

test_normal_branch() {
  new_repo
  "$root/git-extensions.sh" --create >/dev/null && zip_is_valid && zip_has_prefix && zip_comment_empty
}

test_slash_branch() {
  new_repo
  git checkout -q -b fix/slash-branch
  "$root/git-extensions.sh" --create >/dev/null && zip_is_valid
}

test_detached_head() {
  new_repo
  git checkout -q --detach
  "$root/git-extensions.sh" --create >/dev/null && zip_is_valid
}

test_subdirectory() {
  new_repo
  mkdir sub
  (cd sub && "$root/git-extensions.sh" --create >/dev/null) && zip_is_valid
}

test_uncommitted_version_is_ignored() {
  new_repo
  echo '{"name":"acme/myext","version":"9.9.9"}' >composer.json
  "$root/git-extensions.sh" --create >/dev/null && zip_is_valid && [[ ! -e "$tmp/git-exported/acme-myext-9.9.9.zip" ]]
}

test_missing_version_fails() {
  new_repo
  echo '{"name":"acme/myext"}' >composer.json
  git add -A
  commit -m nover
  ! "$root/git-extensions.sh" --create >/dev/null 2>&1 && [[ ! -e "$tmp/git-exported/acme-myext-null.zip" ]]
}

test_failure_keeps_old_zip() {
  new_repo
  "$root/git-extensions.sh" --create >/dev/null || return 1
  local before
  before="$(sha256sum "$zip_file")"
  git rm -q composer.json
  commit -m nocomposer
  ! "$root/git-extensions.sh" --create >/dev/null 2>&1 \
    && [[ "$before" == "$(sha256sum "$zip_file")" ]] && no_partial_files
}

test_outside_repository_fails() {
  cd "$tmp" || return 1
  ! "$root/git-extensions.sh" --create >/dev/null 2>&1
}

# Exit status of a command, so usage errors can be checked for 2.
exit_status() { "$@" >/dev/null 2>&1; echo $?; }

test_extensions_no_arguments_prints_help() {
  new_repo
  local out
  out="$("$root/git-extensions.sh")" && [[ "$out" == Usage:* ]] && [[ ! -e "$zip_file" ]]
}

test_extensions_usage_errors_exit_2() {
  new_repo
  [[ "$(exit_status "$root/git-extensions.sh" --bogus)" == 2 ]] \
    && [[ "$(exit_status "$root/git-extensions.sh" --dry-run)" == 2 ]] \
    && [[ "$(exit_status "$root/git-extensions.sh" --create --dry-run)" == 2 ]] \
    && [[ "$(exit_status "$root/git-extensions.sh" --create --gh-release)" == 2 ]] \
    && [[ ! -e "$zip_file" ]]
}

test_extensions_short_create() {
  new_repo
  "$root/git-extensions.sh" -C >/dev/null && zip_is_valid
}

# Create a fresh style repository in $tmp/style with one commit.
new_style_repo() {
  rm -rf "$tmp/style" "$tmp/git-exported"
  mkdir -p "$tmp/style"
  cd "$tmp/style" || exit 1
  git init -q -b main
  printf 'name = Acme Style\nstyle_version = 1.0.0\n' >style.cfg
  echo hi >a.txt
  git add -A
  commit -m init
}

test_style_create() {
  new_style_repo
  local style_zip="$tmp/git-exported/Acme Style_1.0.0.zip"
  "$root/git-release-style.sh" --create >/dev/null \
    && unzip -tq "$style_zip" >/dev/null && unzip -Z1 "$style_zip" | grep -qx 'Acme Style/a.txt'
}

test_style_usage_errors_exit_2() {
  new_style_repo
  local out
  out="$("$root/git-release-style.sh")" && [[ "$out" == Usage:* ]] \
    && [[ "$(exit_status "$root/git-release-style.sh" --bogus)" == 2 ]] \
    && [[ "$(exit_status "$root/git-release-style.sh" --create --dry-run)" == 2 ]] \
    && [[ "$(exit_status "$root/git-release-style.sh" -C -g)" == 2 ]]
}

test_langpack_usage_errors_exit_2() {
  [[ "$(exit_status "$root/git-release-langpack.sh" --release)" == 2 ]] \
    && [[ "$(exit_status "$root/git-release-langpack.sh" --create --dry-run)" == 2 ]] \
    && [[ "$(exit_status "$root/git-release-langpack.sh" --create --gh-release)" == 2 ]]
}

test_langpack_no_arguments_prints_help() {
  local out
  out="$("$root/git-release-langpack.sh")" && [[ "$out" == Usage:* ]]
}

test_langpack_unknown_option_exits_2() {
  "$root/git-release-langpack.sh" --bogus >/dev/null 2>&1
  [[ $? -eq 2 ]]
}

test_langpack_needs_an_operation() {
  "$root/git-release-langpack.sh" --version 3.3.17 >/dev/null 2>&1
  [[ $? -eq 2 ]]
}

check "git-extensions.sh: normal branch builds a valid zip with prefix and empty comment" test_normal_branch
check "git-extensions.sh: branch name containing a slash" test_slash_branch
check "git-extensions.sh: detached HEAD" test_detached_head
check "git-extensions.sh: run from a subdirectory" test_subdirectory
check "git-extensions.sh: uncommitted composer.json version is ignored" test_uncommitted_version_is_ignored
check "git-extensions.sh: missing version fails, no 'null' zip" test_missing_version_fails
check "git-extensions.sh: failed run keeps the old zip and leaves no partial file" test_failure_keeps_old_zip
check "git-extensions.sh: fails outside a git repository" test_outside_repository_fails
check "git-extensions.sh: no arguments prints help and builds nothing" test_extensions_no_arguments_prints_help
check "git-extensions.sh: usage errors exit 2 and build nothing" test_extensions_usage_errors_exit_2
check "git-extensions.sh: -C is short for --create" test_extensions_short_create
check "git-release-style.sh: --create builds a valid zip with the style folder" test_style_create
check "git-release-style.sh: no arguments prints help; usage errors exit 2" test_style_usage_errors_exit_2
check "git-release-langpack.sh: --release (renamed), --dry-run without --gh-release, and two modes exit 2" test_langpack_usage_errors_exit_2
check "git-release-langpack.sh: no arguments prints help" test_langpack_no_arguments_prints_help
check "git-release-langpack.sh: unknown option exits 2" test_langpack_unknown_option_exits_2
check "git-release-langpack.sh: an operation is required" test_langpack_needs_an_operation

if [[ $failures -gt 0 ]]; then
  echo "$failures test(s) failed."
  exit 1
fi
echo "All tests passed."
