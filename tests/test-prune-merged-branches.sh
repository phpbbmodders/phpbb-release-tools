#!/usr/bin/bash
# Offline checks for prune-merged-branches.py. A local bare repository stands
# in for GitHub (through git's url.insteadOf) and a fake gh answers the two
# questions the script asks: the default branch and the merged PRs of a branch.
# Usage: tests/test-prune-merged-branches.sh
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tool="$root/prune-merged-branches.py"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export GIT_CONFIG_GLOBAL="$work/gitconfig"
export GIT_CONFIG_NOSYSTEM=1
git config --global user.name test
git config --global user.email test@example.invalid
git config --global init.defaultBranch main
git config --global url."file://$work/remote/".insteadOf "https://github.com/"

failures=0
fail() { printf 'FAIL: %s\n' "$1"; failures=$((failures + 1)); }
pass() { printf 'ok: %s\n' "$1"; }
# check NAME CONDITION...: pass when the [[ ]] condition holds.
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
has() { [[ " $2 " == *" $1 "* ]]; }
lacks() { [[ " $2 " != *" $1 "* ]]; }

# Fake gh: "api repos/OWNER/NAME" prints main; "pr list ... --head BRANCH"
# prints $work/prs/BRANCH.json, or [] when there is none.
mkdir -p "$work/bin" "$work/prs"
cat >"$work/bin/gh" <<MOCK
#!/usr/bin/bash
if [[ "\$1" == api ]]; then echo main; exit 0; fi
while [[ \$# -gt 0 ]]; do
	if [[ "\$1" == --head ]]; then cat "$work/prs/\$2.json" 2>/dev/null || echo '[]'; exit 0; fi
	shift
done
exit 1
MOCK
chmod +x "$work/bin/gh"
export PATH="$work/bin:$PATH"

# The "GitHub" repository and a checkout of it.
mkdir -p "$work/remote/acme" "$work/checkouts"
git init -q --bare "$work/remote/acme/demo.git"
git clone -q https://github.com/acme/demo.git "$work/checkouts/demo" 2>/dev/null
repo="$work/checkouts/demo"
git -C "$repo" commit -q --allow-empty -m base
git -C "$repo" push -q origin main

commit_on() {
	# commit_on BRANCH: make BRANCH from main with one commit; print its sha.
	git -C "$repo" switch -q -c "$1" main
	git -C "$repo" commit -q --allow-empty -m "$1"
	git -C "$repo" rev-parse HEAD
	git -C "$repo" switch -q main
}

# Merged normally: main contains its tip.
commit_on merged-normally >/dev/null
git -C "$repo" merge -q --ff-only merged-normally
git -C "$repo" push -q origin main
# Squash-merged from exactly its tip.
tip=$(commit_on squashed)
printf '[{"number": 7, "headRefOid": "%s"}]' "$tip" >"$work/prs/squashed.json"
# Squash-merged, then given another commit: the PR head is the old tip.
old=$(commit_on squashed-then-more)
git -C "$repo" switch -q squashed-then-more
git -C "$repo" commit -q --allow-empty -m "after the merge"
git -C "$repo" switch -q main
printf '[{"number": 8, "headRefOid": "%s"}]' "$old" >"$work/prs/squashed-then-more.json"
# Never merged.
commit_on unmerged >/dev/null
# Merged, but checked out in another worktree.
tip=$(commit_on in-worktree)
printf '[{"number": 9, "headRefOid": "%s"}]' "$tip" >"$work/prs/in-worktree.json"
git -C "$repo" worktree add -q "$work/wt" in-worktree

# A wiki clone and a checkout not on GitHub are skipped without errors.
git init -q --bare "$work/remote/acme/demo.wiki.git"
git clone -q https://github.com/acme/demo.wiki.git "$work/checkouts/demo.wiki" 2>/dev/null
git init -q "$work/checkouts/local-only"

branches() { git -C "$repo" for-each-ref --format='%(refname:short)' refs/heads | sort | tr '\n' ' '; }
before=$(branches)

out=$("$tool" --dry-run "$work/checkouts")
if [[ "$(branches)" == "$before" ]]; then pass "--dry-run deletes nothing"; else fail "--dry-run deletes nothing"; fi
if [[ "$out" == *"== would delete 2, kept 2, failures 0"* ]]; then pass "--dry-run summary"; else fail "--dry-run summary: $out"; fi

status=0
out=$("$tool" "$work/checkouts") || status=$?
after=$(branches)
if [[ $status -eq 0 ]]; then pass "exit 0"; else fail "exit 0 (got $status): $out"; fi
check "a normally merged branch is deleted" lacks merged-normally "$after"
check "a branch squash-merged from its tip is deleted" lacks squashed "$after"
check "a branch with commits after its merge is kept" has squashed-then-more "$after"
check "an unmerged branch is kept" has unmerged "$after"
check "a branch checked out in a worktree is kept" has in-worktree "$after"
check "the default branch is kept" has main "$after"
if [[ "$out" != *demo.wiki* && "$out" != *local-only* ]]; then pass "wiki and non-GitHub checkouts are skipped"; else fail "skipped checkouts: $out"; fi

# A single checkout as the path works too.
out=$("$tool" --dry-run "$repo")
if [[ "$out" == *"== would delete 0, kept 2, failures 0"* ]]; then pass "a single checkout as the path"; else fail "single checkout: $out"; fi

status=0
"$tool" >/dev/null || status=$?
if [[ $status -eq 2 ]]; then pass "no arguments prints help and exits 2"; else fail "no arguments exits 2 (got $status)"; fi

if ((failures)); then
	printf '%d test(s) failed\n' "$failures"
	exit 1
fi
echo "All tests passed."
