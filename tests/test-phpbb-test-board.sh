#!/usr/bin/bash
# Option-handling tests for the phpbb-test-board tools: help output, exit
# status for bad arguments, and the error for a missing board. Does not build
# a board or contact phpBB; the Python scripts' first run creates their
# virtualenv (phpbb-test-board/.venv), which needs network access once.
# Usage: tests/test-phpbb-test-board.sh
# Exit status: 0 if every test passes, 1 otherwise.

set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tools="$root/phpbb-test-board"
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

# Run a command, discard its output, and succeed if it exits with $1.
exits_with() {
  local want="$1"
  shift
  "$@" >/dev/null 2>&1
  [ "$?" -eq "$want" ]
}

# Succeed if the command's combined output contains $1, whatever its exit
# status (captured first, so pipefail doesn't turn a non-zero exit into a miss).
output_has() {
  local text="$1" out
  shift
  out="$("$@" 2>&1)"
  grep -qF -- "$text" <<<"$out"
}

# setup-board.sh
check "setup-board.sh --help exits 0" exits_with 0 "$tools/setup-board.sh" --help
check "setup-board.sh --help shows usage" output_has "Usage: setup-board.sh" "$tools/setup-board.sh" --help
check "setup-board.sh without --dir exits 2" exits_with 2 "$tools/setup-board.sh"
check "setup-board.sh unknown option exits 2" exits_with 2 "$tools/setup-board.sh" --bogus
check "setup-board.sh bad --version exits 2" exits_with 2 "$tools/setup-board.sh" -d "$tmp/board" -v 4.0.0

# smoke_test.py, feature_checks.py and screenshots.py
for script in smoke_test.py feature_checks.py screenshots.py; do
  check "$script --help exits 0" exits_with 0 "$tools/$script" --help
  check "$script without arguments exits 2" exits_with 2 "$tools/$script"
  check "$script with a non-git directory exits 2" exits_with 2 "$tools/$script" "$tmp" -b "$tmp"
done

mkdir -p "$tmp/repo"
git -C "$tmp/repo" init -q
check "smoke_test.py without a board reports how to create one" \
  output_has "setup-board.sh" env -u PHPBB_TEST_BOARD "$tools/smoke_test.py" "$tmp/repo"
check "smoke_test.py without a board exits 1" \
  exits_with 1 env -u PHPBB_TEST_BOARD "$tools/smoke_test.py" "$tmp/repo"
check "feature_checks.py with a missing board.env exits 1" \
  exits_with 1 "$tools/feature_checks.py" "$tmp/repo" -b "$tmp/no-board"
check "feature_checks.py --help lists the extensions with checks" \
  output_has "phpbbmodders/groupwarn" "$tools/feature_checks.py" --help
check "screenshots.py --help lists the extensions with screenshots" \
  output_has "phpbbmodders/documentation" "$tools/screenshots.py" --help
check "screenshots.py without --out exits 2" exits_with 2 "$tools/screenshots.py" "$tmp/repo"
for script in smoke_test.py feature_checks.py screenshots.py; do
  check "$script --help describes --with" output_has "--with PATH[@REF]" "$tools/$script" --help
  check "$script --with a non-git directory exits 2" \
    exits_with 2 "$tools/$script" "$tmp/repo" --with "$tmp" -b "$tmp"
done

if [ "$failures" -eq 0 ]; then
  echo "All tests passed."
else
  echo "$failures test(s) failed."
  exit 1
fi
