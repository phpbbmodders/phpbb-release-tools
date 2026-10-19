#!/usr/bin/bash
# Offline checks for pr-wait.py with a fake gh that plays back scripted responses.
# Usage: tests/test-pr-wait.sh
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE_ROOT=$(mktemp -d)
export FIXTURE_ROOT
trap 'rm -rf -- "$FIXTURE_ROOT"' EXIT
mkdir -p "$FIXTURE_ROOT/bin"

# Fake gh: the Nth call prints responses/N (the last one repeats). A response
# of "FAIL" makes gh exit 1. Every call's arguments are appended to calls.log.
cat >"$FIXTURE_ROOT/bin/gh" <<'MOCK'
#!/usr/bin/bash
set -eu
printf '%s\n' "$*" >>"$FIXTURE_ROOT/calls.log"
count=$(($(cat "$FIXTURE_ROOT/count" 2>/dev/null || echo 0) + 1))
printf '%s\n' "$count" >"$FIXTURE_ROOT/count"
last=$(find "$FIXTURE_ROOT/responses" -type f | wc -l)
if ((count > last)); then count=$last; fi
response=$(cat "$FIXTURE_ROOT/responses/$count")
if [[ "$response" == FAIL ]]; then echo 'gh: simulated failure' >&2; exit 1; fi
printf '%s\n' "$response"
MOCK
chmod +x "$FIXTURE_ROOT/bin/gh"

failures=0
fail() { printf 'FAIL: %s\n' "$1"; failures=$((failures + 1)); }
pass() { printf 'ok: %s\n' "$1"; }

# Build one pull request response: pr STATE HEAD CHECK...
# Each CHECK is run:NAME:STATUS:CONCLUSION or status:CONTEXT:STATE.
pr() {
  local state=$1 head=$2 checks='' item kind name a b
  shift 2
  for item in "$@"; do
    IFS=: read -r kind name a b <<<"$item"
    if [[ "$kind" == run ]]; then
      item="{\"__typename\":\"CheckRun\",\"name\":\"$name\",\"workflowName\":\"CI\",\"status\":\"$a\",\"conclusion\":\"$b\",\"detailsUrl\":\"https://example.test/$name\"}"
    else
      item="{\"__typename\":\"StatusContext\",\"context\":\"$name\",\"state\":\"$a\",\"targetUrl\":\"https://example.test/$name\"}"
    fi
    checks+="${checks:+,}$item"
  done
  printf '{"state":"%s","url":"https://github.com/acme/ext/pull/1","headRefOid":"%s","statusCheckRollup":[%s]}\n' "$state" "$head" "$checks"
}

# Run pr-wait.py against the given responses: scenario NAME EXPECTED_STATUS
# PATTERN [ARGS...] with the responses on stdin, one per line.
scenario() {
  local name=$1 expected=$2 pattern=$3 status=0 n=0 line
  shift 3
  rm -rf "$FIXTURE_ROOT/responses" "$FIXTURE_ROOT/count" "$FIXTURE_ROOT/calls.log"
  mkdir "$FIXTURE_ROOT/responses"
  while IFS= read -r line; do
    n=$((n + 1))
    printf '%s\n' "$line" >"$FIXTURE_ROOT/responses/$n"
  done
  PATH="$FIXTURE_ROOT/bin:$PATH" "$root/pr-wait.py" "$@" >"$FIXTURE_ROOT/out" 2>&1 || status=$?
  if [[ "$status" != "$expected" ]]; then
    fail "$name: exit $status, expected $expected"
    sed 's/^/    /' "$FIXTURE_ROOT/out"
  elif ! grep -qE -- "$pattern" "$FIXTURE_ROOT/out"; then
    fail "$name: output lacks /$pattern/"
    sed 's/^/    /' "$FIXTURE_ROOT/out"
  else
    pass "$name"
  fi
}

A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
B=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
fast=(-i 0.01)

scenario 'no arguments print help' 2 'usage: pr-wait.py' </dev/null
scenario 'an interval of 0 is refused' 2 'must be greater than 0' 1 -i 0 </dev/null
scenario 'a negative timeout is refused' 2 'must be greater than 0' 1 -t -1 </dev/null

scenario 'waits for running checks, then passes' 0 'all 3 checks passed on aaaaaaa' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" run:lint:IN_PROGRESS: run:test:QUEUED:)
$(pr OPEN "$A" run:lint:COMPLETED:SUCCESS run:test:IN_PROGRESS: run:skip:COMPLETED:SKIPPED)
$(pr OPEN "$A" run:lint:COMPLETED:SUCCESS run:test:COMPLETED:NEUTRAL run:skip:COMPLETED:SKIPPED)
EOF

scenario 'lists failed checks with their links' 1 'failure: CI / test  https://example.test/test' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" run:lint:COMPLETED:SUCCESS run:test:COMPLETED:FAILURE)
EOF

scenario 'a cancelled check counts as failed' 1 'cancelled: CI / test' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" run:test:COMPLETED:CANCELLED)
EOF

scenario 'commit statuses wait while pending' 0 'all 2 checks passed' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" status:ci/external:PENDING run:lint:COMPLETED:SUCCESS)
$(pr OPEN "$A" status:ci/external:SUCCESS run:lint:COMPLETED:SUCCESS)
EOF

scenario 'a commit status error fails' 1 'error: ci/external' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" status:ci/external:ERROR)
EOF

scenario 'follows a new push' 0 'all 1 checks passed on bbbbbbb' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" run:test:IN_PROGRESS:)
$(pr OPEN "$B" run:test:QUEUED:)
$(pr OPEN "$B" run:test:COMPLETED:SUCCESS)
EOF
if grep -q 'New commit bbbbbbb pushed' "$FIXTURE_ROOT/out"; then pass 'reports the new push'; else fail 'reports the new push'; fi

scenario 'a failure on the old commit is ignored after a push' 0 'passed on bbbbbbb' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" run:test:IN_PROGRESS: run:lint:COMPLETED:FAILURE)
$(pr OPEN "$B" run:test:COMPLETED:SUCCESS run:lint:COMPLETED:SUCCESS)
EOF

scenario 'stops when the pull request is merged' 3 'is merged; stopped waiting' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A" run:test:IN_PROGRESS:)
$(pr MERGED "$A" run:test:IN_PROGRESS:)
EOF

scenario 'stops when the pull request is closed' 3 'is closed' 1 "${fast[@]}" <<EOF
$(pr CLOSED "$A")
EOF

scenario 'times out while checks run' 4 'Gave up after .*waiting for 1 running checks' 1 -i 0.01 -t 0.002 <<EOF
$(pr OPEN "$A" run:test:IN_PROGRESS:)
EOF

scenario 'gives up when no checks appear' 5 'no checks appeared on aaaaaaa' 1 -i 0.01 -w 0.001 <<EOF
$(pr OPEN "$A")
EOF

scenario 'waits for checks that appear late' 0 'all 1 checks passed' 1 "${fast[@]}" <<EOF
$(pr OPEN "$A")
$(pr OPEN "$A")
$(pr OPEN "$A" run:test:COMPLETED:SUCCESS)
EOF

scenario 'survives a single gh failure' 0 'gh failed \(1/5\)' 1 "${fast[@]}" <<EOF
FAIL
$(pr OPEN "$A" run:test:COMPLETED:SUCCESS)
EOF

scenario 'stops after repeated gh failures' 6 'gh failed \(5/5\): gh: simulated failure' 1 "${fast[@]}" <<EOF
FAIL
EOF

scenario 'treats output that is not JSON as a gh failure' 6 'not JSON' 1 "${fast[@]}" <<EOF
oops
EOF

scenario 'passes the pull request and --repo to gh' 0 'passed' 42 -R acme/ext "${fast[@]}" <<EOF
$(pr OPEN "$A" run:test:COMPLETED:SUCCESS)
EOF
if grep -q '^pr view 42 --json state,url,headRefOid,statusCheckRollup --repo acme/ext$' "$FIXTURE_ROOT/calls.log"; then
  pass 'gh arguments'
else
  fail "gh arguments: $(cat "$FIXTURE_ROOT/calls.log")"
fi

# Without gh on PATH: python3 is found by its full path, and PATH holds nothing else
status=0
PATH="$FIXTURE_ROOT/empty" "$(command -v python3)" "$root/pr-wait.py" 1 -i 0.01 >"$FIXTURE_ROOT/out" 2>&1 || status=$?
if [[ "$status" == 6 ]] && grep -q 'is not installed' "$FIXTURE_ROOT/out" && [[ $(wc -l <"$FIXTURE_ROOT/out") == 1 ]]; then
  pass 'stops at once when gh is not installed'
else
  fail "stops at once when gh is not installed: exit $status, $(cat "$FIXTURE_ROOT/out")"
fi

if ((failures)); then
  printf '%d pr-wait.py check(s) failed\n' "$failures"
  exit 1
fi
echo 'All pr-wait.py checks passed'
