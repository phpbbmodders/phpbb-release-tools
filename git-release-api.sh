#!/usr/bin/bash
# Package remote main and publish a GitHub Release with generated release notes.
# Uses committed composer.json metadata and git archive's export-ignore rules.
# A -dev version is a rolling prerelease: each --release moves its tag to remote
# main and updates the same release's notes and ZIP. Other versions are immutable.
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

Uses remote main, not the local checkout. The composer.json version becomes
the tag and release name. Alpha, beta, RC, and dev versions are prereleases.

Stable, alpha, beta, and RC releases are immutable: an existing release or tag
is rejected, and an upload/publication failure leaves the draft release for
inspection.

A version ending in -dev is a rolling prerelease. The first --release creates
it; each later --release moves its tag to remote main, regenerates its notes,
and replaces its ZIP in the same release, keeping its URL. --dry-run never
changes the tag, release, or asset.
HELP
}

fail() { echo "Error: $*" >&2; exit 1; }
argument_error() { echo "Error: $*" >&2; usage >&2; exit 2; }
api() { timeout 60 gh api --hostname github.com -H 'X-GitHub-Api-Version: 2022-11-28' "$@"; }

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

scratch=$(mktemp -d)
partial=''
release_url=''
completed=false
cleanup() {
  local status=$?
  [[ -z "$partial" ]] || rm -f -- "$partial"
  rm -rf -- "$scratch"
  if [[ "$completed" == false && -n "$release_url" ]]; then
    echo "Release workflow incomplete. Inspect the release before retrying: $release_url" >&2
  fi
  return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

api "repos/$repository" >"$scratch/repository.json"
repository=$(jq -er '.full_name' "$scratch/repository.json")
clone_url=$(jq -er '.clone_url' "$scratch/repository.json")
[[ "$clone_url" == "https://github.com/$repository.git" ]] || fail 'Unexpected GitHub clone URL.'
api "repos/$repository/commits/main" >"$scratch/commit.json"
commit=$(jq -er '.sha | select(test("^[0-9a-f]{40}$"))' "$scratch/commit.json")

# Isolate the remote snapshot so packaging cannot alter the user's checkout.
git init -q "$scratch/source"
timeout 120 git -C "$scratch/source" -c credential.helper= \
  -c 'credential.helper=!gh auth git-credential' fetch -q --depth=1 "$clone_url" "$commit"
[[ "$(git -C "$scratch/source" rev-parse FETCH_HEAD)" == "$commit" ]] || fail 'Fetched commit does not match remote main.'
git -C "$scratch/source" show "$commit:composer.json" >"$scratch/composer.json"
package=$(jq -er '.name | select(type == "string")' "$scratch/composer.json")
version=$(jq -er '.version | select(type == "string")' "$scratch/composer.json")
[[ "$package" =~ ^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$ ]] || fail 'composer.json needs a safe vendor/name package name.'
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-(dev|a(lpha)?[0-9]*|b(eta)?[0-9]*|[Rr][Cc][0-9]*|p(atch)?[0-9]*))?$ ]] || fail 'composer.json has an unsupported version.'
vendor=${package%%/*}
releasename=${package#*/}
filename="$vendor-$releasename-$version.zip"
prerelease=false
if [[ "$version" =~ -(dev|a|b|[Rr][Cc]) ]]; then prerelease=true; fi
rolling=false
if [[ "$version" == *-dev ]]; then rolling=true; fi

# Read every release page, including drafts, before creating anything remotely.
api "repos/$repository/releases?per_page=100" --paginate >"$scratch/releases.json"
jq -e -s 'all(.[]; type == "array")' "$scratch/releases.json" >/dev/null || fail 'Invalid release-list response.'
existing_release_id=''
if jq -e -s --arg tag "$version" 'any(.[][]; .tag_name == $tag)' "$scratch/releases.json" >/dev/null; then
  [[ "$rolling" == true ]] || fail "Release $version already exists; inspect it before retrying."
  # A rolling release is updated in place, so it must be unambiguous.
  existing_release_id=$(jq -er -s --arg tag "$version" \
    '[.[][] | select(.tag_name == $tag)] | select(length == 1) | .[0].id | select(type == "number" and . > 0) | tostring' \
    "$scratch/releases.json") || fail "Expected exactly one release for $version; inspect the releases before retrying."
fi
api "repos/$repository/git/matching-refs/tags/$version" >"$scratch/tags.json"
jq -e 'type == "array"' "$scratch/tags.json" >/dev/null || fail 'Invalid tag-list response.'
existing_tag_sha=''
if jq -e --arg ref "refs/tags/$version" 'any(.[]; .ref == $ref)' "$scratch/tags.json" >/dev/null; then
  [[ "$rolling" == true ]] || fail "Tag $version already exists; update composer.json on main before publishing another version."
  # An annotated tag reports its tag object, which never equals the commit and so counts as moved.
  existing_tag_sha=$(jq -er --arg ref "refs/tags/$version" \
    '.[] | select(.ref == $ref) | .object.sha | select(test("^[0-9a-f]{40}$"))' "$scratch/tags.json") || fail 'Invalid tag response.'
fi

mkdir -p -- "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
partial=$(mktemp "$output_dir/.$filename.XXXXXX.partial")
git -C "$scratch/source" archive --format zip -9 --prefix "$vendor/$releasename/" --output "$partial" "$commit"
zip -q -z "$partial" </dev/null
mv -f -- "$partial" "$output_dir/$filename"
partial=''
echo "Package: $output_dir/$filename"
echo "Source: $repository main at $commit"

# GitHub ignores target_commitish for an existing tag, so notes follow wherever the tag points.
generate_notes() {
  api "repos/$repository/releases/generate-notes" --method POST \
    -f "tag_name=$version" -f "target_commitish=$commit" >"$scratch/notes.json"
  jq -er '.body | select(type == "string")' "$scratch/notes.json" >"$scratch/notes.md"
}
generate_notes
if [[ "$operation" == -n || "$operation" == --dry-run ]]; then
  echo "Release: $version (prerelease: $prerelease)"
  if [[ -n "$existing_release_id" ]]; then
    echo "Would update rolling release $version in place."
  elif [[ "$rolling" == true ]]; then
    echo "Would create rolling release $version."
  fi
  if [[ -n "$existing_tag_sha" && "$existing_tag_sha" != "$commit" ]]; then
    echo "Would move tag $version from $existing_tag_sha to $commit."
    echo 'Note: the preview below reflects the tag'"'"'s current position; --release regenerates the notes after moving it.'
  fi
  echo 'Generated release notes:'
  cat "$scratch/notes.md"
  completed=true
  exit 0
fi

# Rolling -dev: point the tag at the pinned commit, creating it if missing.
if [[ "$rolling" == true && ( -n "$existing_tag_sha" || -n "$existing_release_id" ) ]]; then
  if [[ -n "$existing_release_id" ]]; then
    release_url=$(jq -er -s --arg tag "$version" '[.[][] | select(.tag_name == $tag)][0].html_url' "$scratch/releases.json")
  fi
  if [[ -z "$existing_tag_sha" ]]; then
    api "repos/$repository/git/refs" --method POST -f "ref=refs/tags/$version" -f "sha=$commit" >"$scratch/tag.json"
    generate_notes
  elif [[ "$existing_tag_sha" != "$commit" ]]; then
    api "repos/$repository/git/refs/tags/$version" --method PATCH -f "sha=$commit" -F force=true >"$scratch/tag.json"
    jq -e --arg sha "$commit" '.object.sha == $sha' "$scratch/tag.json" >/dev/null || fail "Tag $version was not moved to $commit."
    echo "Moved tag $version to $commit"
    generate_notes
  fi
fi

if [[ -n "$existing_release_id" ]]; then
  release_id="$existing_release_id"
  echo "Updating rolling release: $release_url"
  # Draft state is left alone here; publication happens only after the new ZIP is in place.
  jq -n --arg tag "$version" --arg commit "$commit" --rawfile body "$scratch/notes.md" \
    '{tag_name:$tag, target_commitish:$commit, name:$tag, body:$body, prerelease:true}' >"$scratch/update.json"
  api "repos/$repository/releases/$release_id" --method PATCH --input "$scratch/update.json" >"$scratch/release.json"
  jq -e --arg tag "$version" '.tag_name == $tag and .prerelease == true' "$scratch/release.json" >/dev/null || fail 'Release update was not confirmed.'

  # Upload under a staging name first so the old ZIP stays available until the new one is confirmed.
  staging="$filename.new"
  api "repos/$repository/releases/$release_id/assets?per_page=100" --paginate >"$scratch/assets.json"
  jq -e -s 'all(.[]; type == "array")' "$scratch/assets.json" >/dev/null || fail 'Invalid asset-list response.'
  # A staging asset left by an interrupted run would block the upload.
  for stale_id in $(jq -r -s --arg name "$staging" '.[][] | select(.name == $name) | .id' "$scratch/assets.json"); do
    api "repos/$repository/releases/assets/$stale_id" --method DELETE >/dev/null
  done
  api "https://uploads.github.com/repos/$repository/releases/$release_id/assets?name=$staging" \
    --method POST -H 'Content-Type: application/zip' --input "$output_dir/$filename" >"$scratch/asset.json"
  size=$(stat -c %s "$output_dir/$filename")
  jq -e --arg name "$staging" --argjson size "$size" \
    '.name == $name and .state == "uploaded" and .size == $size' "$scratch/asset.json" >/dev/null || fail 'Upload response does not match the package.'
  new_asset_id=$(jq -er '.id | select(type == "number" and . > 0) | tostring' "$scratch/asset.json")
  for old_id in $(jq -r -s --arg name "$filename" '.[][] | select(.name == $name) | .id' "$scratch/assets.json"); do
    api "repos/$repository/releases/assets/$old_id" --method DELETE >/dev/null
  done
  api "repos/$repository/releases/assets/$new_asset_id" --method PATCH -f "name=$filename" >"$scratch/renamed.json"
  jq -e --arg name "$filename" '.name == $name' "$scratch/renamed.json" >/dev/null || fail 'Asset rename was not confirmed.'

  api "repos/$repository/releases/$release_id" --method PATCH -F draft=false >"$scratch/published.json"
  jq -e '.draft == false and .prerelease == true' "$scratch/published.json" >/dev/null || fail 'Publication was not confirmed.'
  completed=true
  echo "Updated rolling release: $release_url"
  exit 0
fi

jq -n --arg tag "$version" --arg commit "$commit" --rawfile body "$scratch/notes.md" \
  --argjson prerelease "$prerelease" \
  '{tag_name:$tag, target_commitish:$commit, name:$tag, body:$body, draft:true, prerelease:$prerelease}' >"$scratch/create.json"
# Reserve the tag at the pinned commit; a competing tag creation must fail.
# A rolling tag left by an earlier failed run was already moved above.
if [[ -z "$existing_tag_sha" ]]; then
  api "repos/$repository/git/refs" --method POST -f "ref=refs/tags/$version" -f "sha=$commit" >"$scratch/tag.json"
fi
if ! api "repos/$repository/releases" --method POST --input "$scratch/create.json" >"$scratch/release.json"; then
  echo "Release creation was not confirmed. Inspect https://github.com/$repository/releases and tag $version before retrying." >&2
  exit 1
fi
release_url=$(jq -er '.html_url' "$scratch/release.json")
release_id=$(jq -er '.id | select(type == "number" and . > 0) | tostring' "$scratch/release.json")
echo "Draft release: $release_url"
api "https://uploads.github.com/repos/$repository/releases/$release_id/assets?name=$filename" \
  --method POST -H 'Content-Type: application/zip' --input "$output_dir/$filename" >"$scratch/asset.json"
size=$(stat -c %s "$output_dir/$filename")
jq -e --arg name "$filename" --argjson size "$size" \
  '.name == $name and .state == "uploaded" and .size == $size' "$scratch/asset.json" >/dev/null || fail 'Upload response does not match the package.'
api "repos/$repository/releases/$release_id" --method PATCH -F draft=false >"$scratch/published.json"
jq -e '.draft == false' "$scratch/published.json" >/dev/null || fail 'Publication was not confirmed.'
# A draft's URL uses a temporary untagged-* name; report the published one.
release_url=$(jq -er '.html_url' "$scratch/published.json")
completed=true
echo "Published release: $release_url"
