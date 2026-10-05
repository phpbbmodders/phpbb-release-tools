# shellcheck shell=bash
# Shared GitHub Release publishing for the phpbb-release-tools scripts.
# Source it from a script; it is not meant to be run directly.
#
# A version ending in -dev is a rolling prerelease: publishing it again moves its
# tag to the new commit, regenerates the notes, and replaces the ZIP in the same
# release, keeping its URL. Other versions are immutable: an existing tag or
# release is an error. Alpha, beta, RC, and dev versions are marked as prereleases.
#
# The caller sets GHR_SCRATCH to a private temporary directory and removes it.
# Typical use:
#   ghr_resolve_repository "$repo"        -> GHR_REPOSITORY, $GHR_SCRATCH/repository.json
#   ghr_check_existing "$version"         -> stops early for an immutable duplicate
#   ghr_generate_notes "$version" "$sha"  -> $GHR_SCRATCH/notes.md
#   ghr_preview "$version" "$sha"         (dry run)  or
#   ghr_publish "$version" "$sha" "$zip"  (release)
# On exit, ghr_report_incomplete names a release a failed run left behind.
#
# Requires gh (authenticated to github.com), jq, stat, and timeout.

GHR_REPOSITORY=''
GHR_PRERELEASE=false
GHR_ROLLING=false
GHR_EXISTING_RELEASE_ID=''
GHR_EXISTING_TAG_SHA=''
GHR_RELEASE_URL=''
GHR_COMPLETED=false

ghr_fail() { echo "Error: $*" >&2; exit 1; }
ghr_api() { timeout 60 gh api --hostname github.com -H 'X-GitHub-Api-Version: 2022-11-28' "$@"; }

# Resolve OWNER/REPO (or the current directory's repository when empty) and
# reject archived repositories, which GitHub answers with a misleading 404.
ghr_resolve_repository() {
  local repository="$1"
  if [[ -z "$repository" ]]; then
    repository=$(timeout 60 gh repo view --json nameWithOwner --jq .nameWithOwner) \
      || ghr_fail 'Cannot determine the GitHub repository; use --repo OWNER/REPO.'
  fi
  if [[ ! "$repository" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    ghr_fail 'Repository must be OWNER/REPO on github.com.'
  fi
  ghr_api "repos/$repository" >"$GHR_SCRATCH/repository.json"
  GHR_REPOSITORY=$(jq -er '.full_name' "$GHR_SCRATCH/repository.json")
  if jq -e '.archived == true' "$GHR_SCRATCH/repository.json" >/dev/null; then
    ghr_fail "$GHR_REPOSITORY is archived; unarchive it on GitHub before releasing."
  fi
}

# Stop unless the commit to release is already on GitHub.
ghr_require_remote_commit() {
  local commit="$1"
  ghr_api "repos/$GHR_REPOSITORY/commits/$commit" >/dev/null 2>&1 \
    || ghr_fail "Commit $commit is not on GitHub; push it before releasing."
}

# Classify the version and look up an existing release and tag. Only a rolling
# -dev version may already have them.
ghr_check_existing() {
  local version="$1"
  GHR_PRERELEASE=false
  if [[ "$version" =~ -(dev|a|b|[Rr][Cc]) ]]; then GHR_PRERELEASE=true; fi
  GHR_ROLLING=false
  if [[ "$version" == *-dev ]]; then GHR_ROLLING=true; fi

  # Read every release page, including drafts, before creating anything remotely.
  ghr_api "repos/$GHR_REPOSITORY/releases?per_page=100" --paginate >"$GHR_SCRATCH/releases.json"
  jq -e -s 'all(.[]; type == "array")' "$GHR_SCRATCH/releases.json" >/dev/null || ghr_fail 'Invalid release-list response.'
  GHR_EXISTING_RELEASE_ID=''
  if jq -e -s --arg tag "$version" 'any(.[][]; .tag_name == $tag)' "$GHR_SCRATCH/releases.json" >/dev/null; then
    [[ "$GHR_ROLLING" == true ]] || ghr_fail "Release $version already exists; inspect it before retrying."
    # A rolling release is updated in place, so it must be unambiguous.
    GHR_EXISTING_RELEASE_ID=$(jq -er -s --arg tag "$version" \
      '[.[][] | select(.tag_name == $tag)] | select(length == 1) | .[0].id | select(type == "number" and . > 0) | tostring' \
      "$GHR_SCRATCH/releases.json") || ghr_fail "Expected exactly one release for $version; inspect the releases before retrying."
  fi
  ghr_api "repos/$GHR_REPOSITORY/git/matching-refs/tags/$version" >"$GHR_SCRATCH/tags.json"
  jq -e 'type == "array"' "$GHR_SCRATCH/tags.json" >/dev/null || ghr_fail 'Invalid tag-list response.'
  GHR_EXISTING_TAG_SHA=''
  if jq -e --arg ref "refs/tags/$version" 'any(.[]; .ref == $ref)' "$GHR_SCRATCH/tags.json" >/dev/null; then
    [[ "$GHR_ROLLING" == true ]] || ghr_fail "Tag $version already exists; change the version before publishing another release."
    # An annotated tag reports its tag object, which never equals the commit and so counts as moved.
    GHR_EXISTING_TAG_SHA=$(jq -er --arg ref "refs/tags/$version" \
      '.[] | select(.ref == $ref) | .object.sha | select(test("^[0-9a-f]{40}$"))' "$GHR_SCRATCH/tags.json") || ghr_fail 'Invalid tag response.'
  fi
}

# GitHub ignores target_commitish for an existing tag, so notes follow wherever the tag points.
ghr_generate_notes() {
  local version="$1" commit="$2"
  ghr_api "repos/$GHR_REPOSITORY/releases/generate-notes" --method POST \
    -f "tag_name=$version" -f "target_commitish=$commit" >"$GHR_SCRATCH/notes.json"
  jq -er '.body | select(type == "string")' "$GHR_SCRATCH/notes.json" >"$GHR_SCRATCH/notes.md"
}

# Describe what a release would do, without changing anything on GitHub.
ghr_preview() {
  local version="$1" commit="$2"
  echo "Release: $version (prerelease: $GHR_PRERELEASE)"
  if [[ -n "$GHR_EXISTING_RELEASE_ID" ]]; then
    echo "Would update rolling release $version in place."
  elif [[ "$GHR_ROLLING" == true ]]; then
    echo "Would create rolling release $version."
  fi
  if [[ -n "$GHR_EXISTING_TAG_SHA" && "$GHR_EXISTING_TAG_SHA" != "$commit" ]]; then
    echo "Would move tag $version from $GHR_EXISTING_TAG_SHA to $commit."
    echo "Note: the preview below reflects the tag's current position; a release regenerates the notes after moving it."
  fi
  echo 'Generated release notes:'
  cat "$GHR_SCRATCH/notes.md"
}

# Upload a ZIP to a release and confirm GitHub stored it intact.
ghr_upload_asset() {
  local release_id="$1" file="$2" name="$3" size
  ghr_api "https://uploads.github.com/repos/$GHR_REPOSITORY/releases/$release_id/assets?name=$name" \
    --method POST -H 'Content-Type: application/zip' --input "$file" >"$GHR_SCRATCH/asset.json"
  size=$(stat -c %s "$file")
  jq -e --arg name "$name" --argjson size "$size" \
    '.name == $name and .state == "uploaded" and .size == $size' "$GHR_SCRATCH/asset.json" >/dev/null || ghr_fail 'Upload response does not match the package.'
}

# Publish the ZIP: create a new release, or update the rolling -dev release.
ghr_publish() {
  local version="$1" commit="$2" zip="$3"
  local filename
  filename=$(basename -- "$zip")

  # Rolling -dev: point the tag at the pinned commit, creating it if missing.
  if [[ "$GHR_ROLLING" == true && ( -n "$GHR_EXISTING_TAG_SHA" || -n "$GHR_EXISTING_RELEASE_ID" ) ]]; then
    if [[ -n "$GHR_EXISTING_RELEASE_ID" ]]; then
      GHR_RELEASE_URL=$(jq -er -s --arg tag "$version" '[.[][] | select(.tag_name == $tag)][0].html_url' "$GHR_SCRATCH/releases.json")
    fi
    if [[ -z "$GHR_EXISTING_TAG_SHA" ]]; then
      ghr_api "repos/$GHR_REPOSITORY/git/refs" --method POST -f "ref=refs/tags/$version" -f "sha=$commit" >"$GHR_SCRATCH/tag.json"
      ghr_generate_notes "$version" "$commit"
    elif [[ "$GHR_EXISTING_TAG_SHA" != "$commit" ]]; then
      ghr_api "repos/$GHR_REPOSITORY/git/refs/tags/$version" --method PATCH -f "sha=$commit" -F force=true >"$GHR_SCRATCH/tag.json"
      jq -e --arg sha "$commit" '.object.sha == $sha' "$GHR_SCRATCH/tag.json" >/dev/null || ghr_fail "Tag $version was not moved to $commit."
      echo "Moved tag $version to $commit"
      ghr_generate_notes "$version" "$commit"
    fi
  fi

  if [[ -n "$GHR_EXISTING_RELEASE_ID" ]]; then
    ghr_update_rolling "$version" "$commit" "$zip" "$filename"
    return
  fi

  local release_id
  jq -n --arg tag "$version" --arg commit "$commit" --rawfile body "$GHR_SCRATCH/notes.md" \
    --argjson prerelease "$GHR_PRERELEASE" \
    '{tag_name:$tag, target_commitish:$commit, name:$tag, body:$body, draft:true, prerelease:$prerelease}' >"$GHR_SCRATCH/create.json"
  # Reserve the tag at the pinned commit; a competing tag creation must fail.
  # A rolling tag left by an earlier failed run was already moved above.
  if [[ -z "$GHR_EXISTING_TAG_SHA" ]]; then
    ghr_api "repos/$GHR_REPOSITORY/git/refs" --method POST -f "ref=refs/tags/$version" -f "sha=$commit" >"$GHR_SCRATCH/tag.json"
  fi
  if ! ghr_api "repos/$GHR_REPOSITORY/releases" --method POST --input "$GHR_SCRATCH/create.json" >"$GHR_SCRATCH/release.json"; then
    echo "Release creation was not confirmed. Inspect https://github.com/$GHR_REPOSITORY/releases and tag $version before retrying." >&2
    exit 1
  fi
  GHR_RELEASE_URL=$(jq -er '.html_url' "$GHR_SCRATCH/release.json")
  release_id=$(jq -er '.id | select(type == "number" and . > 0) | tostring' "$GHR_SCRATCH/release.json")
  echo "Draft release: $GHR_RELEASE_URL"
  ghr_upload_asset "$release_id" "$zip" "$filename"
  ghr_api "repos/$GHR_REPOSITORY/releases/$release_id" --method PATCH -F draft=false >"$GHR_SCRATCH/published.json"
  jq -e '.draft == false' "$GHR_SCRATCH/published.json" >/dev/null || ghr_fail 'Publication was not confirmed.'
  # A draft's URL uses a temporary untagged-* name; report the published one.
  GHR_RELEASE_URL=$(jq -er '.html_url' "$GHR_SCRATCH/published.json")
  GHR_COMPLETED=true
  echo "Published release: $GHR_RELEASE_URL"
}

# Update the existing rolling release in place: notes, then the ZIP, then publish.
ghr_update_rolling() {
  local version="$1" commit="$2" zip="$3" filename="$4"
  local release_id="$GHR_EXISTING_RELEASE_ID" staging new_asset_id stale_id old_id
  echo "Updating rolling release: $GHR_RELEASE_URL"
  # Draft state is left alone here; publication happens only after the new ZIP is in place.
  jq -n --arg tag "$version" --arg commit "$commit" --rawfile body "$GHR_SCRATCH/notes.md" \
    '{tag_name:$tag, target_commitish:$commit, name:$tag, body:$body, prerelease:true}' >"$GHR_SCRATCH/update.json"
  ghr_api "repos/$GHR_REPOSITORY/releases/$release_id" --method PATCH --input "$GHR_SCRATCH/update.json" >"$GHR_SCRATCH/release.json"
  jq -e --arg tag "$version" '.tag_name == $tag and .prerelease == true' "$GHR_SCRATCH/release.json" >/dev/null || ghr_fail 'Release update was not confirmed.'

  # Upload under a staging name first so the old ZIP stays available until the new one is confirmed.
  staging="$filename.new"
  ghr_api "repos/$GHR_REPOSITORY/releases/$release_id/assets?per_page=100" --paginate >"$GHR_SCRATCH/assets.json"
  jq -e -s 'all(.[]; type == "array")' "$GHR_SCRATCH/assets.json" >/dev/null || ghr_fail 'Invalid asset-list response.'
  # A staging asset left by an interrupted run would block the upload.
  for stale_id in $(jq -r -s --arg name "$staging" '.[][] | select(.name == $name) | .id' "$GHR_SCRATCH/assets.json"); do
    ghr_api "repos/$GHR_REPOSITORY/releases/assets/$stale_id" --method DELETE >/dev/null
  done
  ghr_upload_asset "$release_id" "$zip" "$staging"
  new_asset_id=$(jq -er '.id | select(type == "number" and . > 0) | tostring' "$GHR_SCRATCH/asset.json")
  for old_id in $(jq -r -s --arg name "$filename" '.[][] | select(.name == $name) | .id' "$GHR_SCRATCH/assets.json"); do
    ghr_api "repos/$GHR_REPOSITORY/releases/assets/$old_id" --method DELETE >/dev/null
  done
  ghr_api "repos/$GHR_REPOSITORY/releases/assets/$new_asset_id" --method PATCH -f "name=$filename" >"$GHR_SCRATCH/renamed.json"
  jq -e --arg name "$filename" '.name == $name' "$GHR_SCRATCH/renamed.json" >/dev/null || ghr_fail 'Asset rename was not confirmed.'

  ghr_api "repos/$GHR_REPOSITORY/releases/$release_id" --method PATCH -F draft=false >"$GHR_SCRATCH/published.json"
  jq -e '.draft == false and .prerelease == true' "$GHR_SCRATCH/published.json" >/dev/null || ghr_fail 'Publication was not confirmed.'
  GHR_COMPLETED=true
  echo "Updated rolling release: $GHR_RELEASE_URL"
}

# For an EXIT trap: name a release that a failed run left in an unknown state.
ghr_report_incomplete() {
  if [[ "$GHR_COMPLETED" == false && -n "$GHR_RELEASE_URL" ]]; then
    echo "Release workflow incomplete. Inspect the release before retrying: $GHR_RELEASE_URL" >&2
  fi
}
