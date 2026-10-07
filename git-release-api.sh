#!/usr/bin/bash
# Package the remote default branch and publish a GitHub Release with generated release notes.
# Uses committed composer.json metadata and git archive's export-ignore rules.
# A -dev version is a rolling prerelease: each --release moves its tag to the
# remote default branch and updates the same release's notes and ZIP. Other
# versions are immutable.
# Requires git, gh (authenticated to github.com), jq, zip, and timeout.
# Usage: git-release-api.sh --dry-run|--release [--repo OWNER/REPO] [--output DIR]
# Exit status: 0 on success, 1 on operational failure, 2 on invalid arguments.

set -Eeuo pipefail

usage() {
  cat <<'HELP'
Usage: git-release-api.sh --dry-run|--release [options]

  -n, --dry-run          Build the ZIP and preview generated notes; do not publish.
  -r, --release          Create a release, upload the ZIP, then publish it
                         (or update the rolling -dev release).
  -R, --repo OWNER/REPO  GitHub repository (defaults to the current repository).
  -o, --output DIR      ZIP destination (defaults to ../git-exported).
  -h, --help            Show help.

Uses the repository's default branch on GitHub (for example main or master),
not the local checkout. The composer.json version becomes
the tag and release name. Alpha, beta, RC, and dev versions are prereleases.

Stable, alpha, beta, and RC releases are immutable: an existing release or tag
is rejected, and an upload/publication failure leaves the draft release for
inspection.

A version ending in -dev, such as 1.1.0-dev or 1.1.0-a2-dev, is a rolling
prerelease. The first --release creates
it; each later --release moves its tag to the default branch, regenerates its notes,
and replaces its ZIP in the same release, keeping its URL. --dry-run never
changes the tag, release, or asset.
HELP
}

fail() { echo "Error: $*" >&2; exit 1; }
argument_error() { echo "Error: $*" >&2; usage >&2; exit 2; }

# Shared release logic lives next to the real script, even when it is run through a symlink.
script_dir=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")
# shellcheck source=lib/github-release.sh
source "$script_dir/lib/github-release.sh"

operation=''
repository=''
output_dir=''
if (($# == 0)); then usage; exit 0; fi
while (($#)); do
  case "$1" in
    -n|--dry-run|-r|--release)
      [[ -z "$operation" ]] || argument_error 'Choose exactly one operation.'
      operation="$1"
      shift
      ;;
    -R|--repo|-o|--output)
      if (($# < 2)); then argument_error "Missing value for $1."; fi
      if [[ -z "$2" || "$2" == -* ]]; then argument_error "Missing value for $1."; fi
      if [[ "$1" == -R || "$1" == --repo ]]; then repository="$2"; else output_dir="$2"; fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) argument_error "Unknown option: $1" ;;
  esac
done
[[ -n "$operation" ]] || argument_error 'Choose --dry-run or --release.'
for dependency in git gh jq zip timeout; do
  command -v "$dependency" >/dev/null || fail "Required command not found: $dependency"
done
if [[ -z "$repository" ]]; then
  repository=$(timeout 60 gh repo view --json nameWithOwner --jq .nameWithOwner) || fail 'Cannot determine the GitHub repository; use --repo OWNER/REPO.'
fi
[[ "$repository" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || argument_error 'Repository must be OWNER/REPO on github.com.'
if [[ -z "$output_dir" ]]; then
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || repo_root="$PWD"
  output_dir="$repo_root/../git-exported"
fi

GHR_SCRATCH=$(mktemp -d)
scratch="$GHR_SCRATCH"
partial=''
cleanup() {
  local status=$?
  [[ -z "$partial" ]] || rm -f -- "$partial"
  rm -rf -- "$scratch"
  ghr_report_incomplete
  return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ghr_resolve_repository "$repository"
repository="$GHR_REPOSITORY"
clone_url=$(jq -er '.clone_url' "$scratch/repository.json")
[[ "$clone_url" == "https://github.com/$repository.git" ]] || fail 'Unexpected GitHub clone URL.'
branch=$(jq -er '.default_branch | select(type == "string")' "$scratch/repository.json")
# Reject anything git would not accept as a branch name before using it in a URL.
git check-ref-format --branch "$branch" >/dev/null 2>&1 || fail 'Unexpected default branch name.'
[[ "$branch" =~ ^[A-Za-z0-9._/-]+$ ]] || fail 'Unexpected default branch name.'
ghr_api "repos/$repository/commits/$branch" >"$scratch/commit.json"
commit=$(jq -er '.sha | select(test("^[0-9a-f]{40}$"))' "$scratch/commit.json")

# Isolate the remote snapshot so packaging cannot alter the user's checkout.
git init -q "$scratch/source"
timeout 120 git -C "$scratch/source" -c credential.helper= \
  -c 'credential.helper=!gh auth git-credential' fetch -q --depth=1 "$clone_url" "$commit"
[[ "$(git -C "$scratch/source" rev-parse FETCH_HEAD)" == "$commit" ]] || fail "Fetched commit does not match remote $branch."
git -C "$scratch/source" show "$commit:composer.json" >"$scratch/composer.json"
package=$(jq -er '.name | select(type == "string")' "$scratch/composer.json")
version=$(jq -er '.version | select(type == "string")' "$scratch/composer.json")
[[ "$package" =~ ^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$ ]] || fail 'composer.json needs a safe vendor/name package name.'
# An alpha, beta or RC may end in -dev (1.1.0-a2-dev): a rolling build on the way to it.
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-(dev|(a(lpha)?|b(eta)?|[Rr][Cc])[0-9]*(-dev)?|p(atch)?[0-9]*))?$ ]] || fail 'composer.json has an unsupported version.'
vendor=${package%%/*}
releasename=${package#*/}
filename="$vendor-$releasename-$version.zip"
ghr_check_existing "$version"

mkdir -p -- "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
partial=$(mktemp "$output_dir/.$filename.XXXXXX.partial")
git -C "$scratch/source" archive --format zip -9 --prefix "$vendor/$releasename/" --output "$partial" "$commit"
zip -q -z "$partial" </dev/null
mv -f -- "$partial" "$output_dir/$filename"
partial=''
echo "Package: $output_dir/$filename"
echo "Source: $repository $branch at $commit"

ghr_generate_notes "$version" "$commit"
if [[ "$operation" == -n || "$operation" == --dry-run ]]; then
  ghr_preview "$version" "$commit"
  GHR_COMPLETED=true
  exit 0
fi
ghr_publish "$version" "$commit" "$output_dir/$filename"
