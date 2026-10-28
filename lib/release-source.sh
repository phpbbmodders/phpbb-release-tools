# shellcheck shell=bash
# Shared source selection for the three package release scripts.
# Call release_prepare_source with create/gh-release (or empty for validation).
# It pins RELEASE_COMMIT, sets RELEASE_OUTPUT, and changes into the source repo.
# Remote snapshots live under GHR_SCRATCH, removed by the caller's EXIT trap.

RELEASE_REMOTE=false
RELEASE_REPO=''
RELEASE_OUTPUT=''
RELEASE_COMMIT=''

release_usage() {
  cat <<EOF
Usage:
  $1 --create [options]
  $1 --release [options]

  -C, --create          Build a ZIP only.
  -r, --release         Build a ZIP and publish it to GitHub Releases.
      --remote          Use GitHub's default branch instead of local HEAD.
  -n, --dry-run         With --release, build and preview without GitHub writes.
  -f, --force           With --release, replace an existing tag/release ZIP.
  -R, --repo OWNER/REPO Select the repository for --remote; otherwise infer it.
  -o, --output DIR      ZIP destination (default: ../git-exported).
  -h, --help            Show help; no arguments also shows help.

Local publication requires clean tracked files and an already-pushed HEAD.
Rolling -dev releases update in place without --force. Other existing versions
require --force. GitHub-enforced immutability cannot be overridden.
Dry runs can replace a local ZIP but never a GitHub tag, release, or asset.
Legacy aliases: -g/--gh-release for --release; --output-dir for --output.
EOF
}

release_option_value() {
  if (($# < 2)) || [[ -z "$2" || "$2" == -* ]]; then
    echo "Error: $1 needs a value" >&2
    exit 2
  fi
}

release_validate_options() {
  local mode="$1"
  if [[ -n "$RELEASE_REPO" && "$RELEASE_REMOTE" != true ]]; then
    echo 'Error: --repo requires --remote' >&2
    exit 2
  fi
  if [[ "$GHR_FORCE" == true && "$mode" != gh-release ]]; then
    echo 'Error: --force requires --release' >&2
    exit 2
  fi
}

release_prepare_source() {
  local mode="$1" base clone_url branch command
  base=$(git rev-parse --show-toplevel 2>/dev/null) || base="$PWD"
  if [[ -z "$RELEASE_OUTPUT" ]]; then
    RELEASE_OUTPUT="$base/../git-exported"
  elif [[ "$RELEASE_OUTPUT" != /* ]]; then
    RELEASE_OUTPUT="$base/$RELEASE_OUTPUT"
  fi

  if [[ "$RELEASE_REMOTE" == false ]]; then
    git rev-parse --show-toplevel >/dev/null 2>&1 \
      || ghr_fail 'the current directory is not inside a git repository'
    cd "$base" || ghr_fail 'cannot enter the repository root'
    RELEASE_COMMIT=$(git rev-parse --verify 'HEAD^{commit}') || ghr_fail 'the repository has no commits'
    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
      [[ "$mode" != gh-release ]] || ghr_fail 'tracked files contain uncommitted changes'
      echo 'Warning: uncommitted tracked changes are not included; only HEAD is packaged.' >&2
    fi
    return
  fi

  for command in gh jq timeout; do
    command -v "$command" >/dev/null || ghr_fail "required command not found: $command"
  done
  gh auth status >/dev/null 2>&1 || ghr_fail 'GitHub CLI is not authenticated; run: gh auth login'
  GHR_SCRATCH=$(mktemp -d)
  ghr_resolve_repository "$RELEASE_REPO"
  clone_url=$(jq -er '.clone_url' "$GHR_SCRATCH/repository.json")
  [[ "$clone_url" == "https://github.com/$GHR_REPOSITORY.git" ]] || ghr_fail 'Unexpected GitHub clone URL.'
  branch=$(jq -er '.default_branch | select(type == "string")' "$GHR_SCRATCH/repository.json")
  git check-ref-format --branch "$branch" >/dev/null 2>&1 || ghr_fail 'Unexpected default branch name.'
  [[ "$branch" =~ ^[A-Za-z0-9._/-]+$ ]] || ghr_fail 'Unexpected default branch name.'
  ghr_api "repos/$GHR_REPOSITORY/commits/$branch" >"$GHR_SCRATCH/commit.json"
  RELEASE_COMMIT=$(jq -er '.sha | select(test("^[0-9a-f]{40}$"))' "$GHR_SCRATCH/commit.json")

  git init -q "$GHR_SCRATCH/source"
  timeout 120 git -C "$GHR_SCRATCH/source" -c credential.helper= \
    -c 'credential.helper=!gh auth git-credential' fetch -q --depth=1 "$clone_url" "$RELEASE_COMMIT"
  [[ "$(git -C "$GHR_SCRATCH/source" rev-parse FETCH_HEAD)" == "$RELEASE_COMMIT" ]] \
    || ghr_fail 'Fetched commit does not match the pinned remote commit.'
  # Only Git's object database is needed for metadata and git archive.
  git -C "$GHR_SCRATCH/source" update-ref HEAD "$RELEASE_COMMIT"
  cd "$GHR_SCRATCH/source" || ghr_fail 'cannot enter the remote snapshot'
  echo "Source: $GHR_REPOSITORY $branch at $RELEASE_COMMIT"
}
