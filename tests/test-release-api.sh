#!/usr/bin/bash
# Offline release workflow checks using a real Git fixture and a mocked GitHub API.
# Covers local/remote sources, publication, rolling releases, and forced replacement.
# Usage: tests/test-release-api.sh
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE_ROOT=$(mktemp -d)
export FIXTURE_ROOT
trap 'rm -rf -- "$FIXTURE_ROOT"' EXIT
REAL_GIT=$(command -v git)
export REAL_GIT
mkdir -p "$FIXTURE_ROOT/bin" "$FIXTURE_ROOT/source"
git init -q -b main "$FIXTURE_ROOT/source"
printf '%s\n' '{"name":"acme/ext","version":"1.2.3-a1"}' >"$FIXTURE_ROOT/source/composer.json"
printf '%s\n' 'Included fixture' >"$FIXTURE_ROOT/source/included.txt"
printf '%s\n' 'Excluded fixture' >"$FIXTURE_ROOT/source/excluded.txt"
printf '%s\n' 'excluded.txt export-ignore' >"$FIXTURE_ROOT/source/.gitattributes"
git -C "$FIXTURE_ROOT/source" add -A
git -C "$FIXTURE_ROOT/source" -c user.name='William Jacoby' -c user.email=bonelifer@gmail.com commit -qm fixture
FIXTURE_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse HEAD)
export FIXTURE_COMMIT
# A rolling -dev commit on its own branch, so both commits stay fetchable tips.
git -C "$FIXTURE_ROOT/source" checkout -qb dev
printf '%s\n' '{"name":"acme/ext","version":"1.2.3-dev"}' >"$FIXTURE_ROOT/source/composer.json"
git -C "$FIXTURE_ROOT/source" -c user.name='William Jacoby' -c user.email=bonelifer@gmail.com commit -qam dev
DEV_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse HEAD)
export DEV_COMMIT
# A rolling build on the way to an alpha, also on its own branch.
git -C "$FIXTURE_ROOT/source" checkout -qb alpha-dev main
printf '%s\n' '{"name":"acme/ext","version":"1.2.3-a2-dev"}' >"$FIXTURE_ROOT/source/composer.json"
git -C "$FIXTURE_ROOT/source" -c user.name='William Jacoby' -c user.email=bonelifer@gmail.com commit -qam alpha-dev
ALPHA_DEV_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse HEAD)
export ALPHA_DEV_COMMIT
git -C "$FIXTURE_ROOT/source" checkout -qb stable main
printf '%s\n' '{"name":"acme/ext","version":"1.2.3"}' >"$FIXTURE_ROOT/source/composer.json"
git -C "$FIXTURE_ROOT/source" -c user.name=test -c user.email=test@example.com commit -qam stable
STABLE_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse HEAD)
export STABLE_COMMIT
git -C "$FIXTURE_ROOT/source" checkout -q main
# A dirty local version must never change the remotely committed package version.
printf '%s\n' '{"name":"acme/ext","version":"9.9.9"}' >"$FIXTURE_ROOT/source/composer.json"

cat >"$FIXTURE_ROOT/bin/git" <<'MOCK'
#!/usr/bin/bash
set -eu
args=("$@")
for index in "${!args[@]}"; do
  if [[ "${args[$index]}" == https://github.com/acme/ext.git ]]; then
    args[$index]="$FIXTURE_ROOT/source"
  fi
done
if [[ "${SCENARIO:-}" == archive_failure && " $* " == *' archive '* ]]; then exit 1; fi
exec "$REAL_GIT" "${args[@]}"
MOCK
cat >"$FIXTURE_ROOT/bin/gh" <<'MOCK'
#!/usr/bin/bash
set -eu
if [[ "$1" == repo ]]; then echo acme/ext; exit 0; fi
if [[ "$1" == auth ]]; then exit 0; fi
endpoint=''
input=''
method=GET
previous=''
for value in "$@"; do
  case "$value" in repos/*|https://uploads.github.com/*) endpoint="$value" ;; esac
  if [[ "$previous" == --input ]]; then input="$value"; fi
  if [[ "$previous" == --method ]]; then method="$value"; fi
  previous="$value"
done
echo "$endpoint ${input:+with-input}" >>"$FIXTURE_ROOT/api.log"
echo "$method $endpoint" >>"$FIXTURE_ROOT/methods.log"
# Forced-release fixtures use a stable version unless a package case overrides it.
if [[ "${SCENARIO:-}" == force_* ]]; then
  version="${FORCE_VERSION:-1.2.3}"
  asset="${FORCE_ASSET:-acme-ext-$version.zip}"
  prerelease=false
  if [[ "$version" == *-* ]]; then prerelease=true; fi
  case "$endpoint" in
    'repos/acme/ext/releases?per_page=100')
      if [[ "$SCENARIO" == force_tag_only ]]; then echo '[]'; else
        immutable=false
        if [[ "$SCENARIO" == force_immutable ]]; then immutable=true; fi
        jq -n --arg version "$version" --argjson immutable "$immutable" \
          '[{tag_name:$version,id:789,html_url:("https://github.com/acme/ext/releases/tag/"+$version),immutable:$immutable}]'
      fi
      exit 0 ;;
    repos/acme/ext/git/matching-refs/tags/*)
      if [[ "$SCENARIO" == force_release_only ]]; then echo '[]'; else
        jq -n --arg version "$version" --arg sha "$FIXTURE_COMMIT" \
          '[{ref:("refs/tags/"+$version),object:{type:"tag",sha:$sha}}]'
      fi
      exit 0 ;;
    repos/acme/ext/git/refs/tags/*)
      [[ "$SCENARIO" != force_tag_failure ]] || exit 1
      [[ " $* " == *' force=true '* ]] || exit 1
      jq -n --arg sha "${REMOTE_COMMIT:-$STABLE_COMMIT}" '{object:{sha:$sha}}'
      exit 0 ;;
    repos/acme/ext/releases/789)
      if [[ -n "$input" ]]; then
        cp "$input" "$FIXTURE_ROOT/update.json"
        jq -n --arg version "$version" --argjson pre "$prerelease" '{tag_name:$version,prerelease:$pre}'
      else
        [[ "$SCENARIO" != force_publish_failure ]] || exit 1
        jq -n --argjson pre "$prerelease" '{draft:false,prerelease:$pre}'
      fi
      exit 0 ;;
    'repos/acme/ext/releases/789/assets?per_page=100')
      jq -n --arg name "$asset" '[{id:7,name:$name},{id:8,name:($name+".new")},{id:10,name:"other-asset.zip"}]'
      exit 0 ;;
    repos/acme/ext/releases/assets/9)
      jq -n --arg name "$asset" '{name:$name}'
      exit 0 ;;
  esac
fi
case "$endpoint" in
  repos/acme/ext)
    if [[ "${SCENARIO:-}" == master_branch ]]; then branch=master; else branch=main; fi
    if [[ "${SCENARIO:-}" == archived ]]; then archived=true; else archived=false; fi
    jq -n --arg branch "$branch" --argjson archived "$archived" \
      '{full_name:"acme/ext",clone_url:"https://github.com/acme/ext.git",default_branch:$branch,archived:$archived}' ;;
  repos/acme/ext/commits/[0-9a-f]*)
    # A local HEAD is only "on GitHub" when the fixture has it.
    "$REAL_GIT" -C "$FIXTURE_ROOT/source" cat-file -e "${endpoint##*/}^{commit}" 2>/dev/null || exit 1
    echo '{}' ;;
  repos/acme/ext/commits/main|repos/acme/ext/commits/master)
    [[ "$endpoint" == */main || "${SCENARIO:-}" == master_branch ]] || exit 1
    [[ "${SCENARIO:-}" != main_failure ]] || exit 1
    if [[ -n "${REMOTE_COMMIT:-}" ]]; then sha="$REMOTE_COMMIT"
    elif [[ "${SCENARIO:-}" == force_* ]]; then sha="$STABLE_COMMIT"
    elif [[ "${SCENARIO:-}" == dev_* ]]; then sha="$DEV_COMMIT"
    elif [[ "${SCENARIO:-}" == alpha_dev ]]; then sha="$ALPHA_DEV_COMMIT"
    else sha="$FIXTURE_COMMIT"; fi
    jq -n --arg sha "$sha" '{sha:$sha}' ;;
  'repos/acme/ext/releases?per_page=100')
    echo '[]'
    case "${SCENARIO:-}" in
      existing_release) echo '[{"tag_name":"1.2.3-a1"}]' ;;
      dev_existing) echo '[{"tag_name":"1.2.3-dev","id":456,"html_url":"https://github.com/acme/ext/releases/tag/1.2.3-dev"}]' ;;
      *) echo '[]' ;;
    esac ;;
  repos/acme/ext/git/matching-refs/tags/1.2.3-a1)
    if [[ "${SCENARIO:-}" == existing_tag ]]; then echo '[{"ref":"refs/tags/1.2.3-a1"}]'; else echo '[]'; fi ;;
  repos/acme/ext/git/matching-refs/tags/1.2.3-dev)
    # The existing rolling tag still points at an older commit.
    if [[ "${SCENARIO:-}" == dev_existing ]]; then
      jq -n --arg sha "$FIXTURE_COMMIT" '[{ref:"refs/tags/1.2.3-dev",object:{sha:$sha}}]'
    else echo '[]'; fi ;;
  repos/acme/ext/git/matching-refs/tags/1.2.3-a2-dev)
    echo '[]' ;;
  repos/acme/ext/git/refs/tags/1.2.3-dev)
    jq -n --arg sha "$DEV_COMMIT" '{ref:"refs/tags/1.2.3-dev",object:{sha:$sha}}' ;;
  repos/acme/ext/releases/456)
    if [[ -n "$input" ]]; then
      cp "$input" "$FIXTURE_ROOT/update.json"
      echo '{"tag_name":"1.2.3-dev","prerelease":true}'
    else echo '{"draft":false,"prerelease":true}'; fi ;;
  'repos/acme/ext/releases/456/assets?per_page=100')
    echo '[{"id":7,"name":"acme-ext-1.2.3-dev.zip"},{"id":8,"name":"acme-ext-1.2.3-dev.zip.new"}]' ;;
  repos/acme/ext/releases/assets/7|repos/acme/ext/releases/assets/8)
    echo '' ;;
  repos/acme/ext/releases/assets/9)
    echo '{"name":"acme-ext-1.2.3-dev.zip"}' ;;
  repos/acme/ext/releases/generate-notes)
    echo '{"body":"## Changes\n\n* Example merged change"}' ;;
  repos/acme/ext/git/refs)
    [[ "${SCENARIO:-}" != tag_failure ]] || exit 1
    echo '{"ref":"refs/tags/1.2.3-a1"}' ;;
  repos/acme/ext/releases)
    cp "$input" "$FIXTURE_ROOT/create.json"
    echo '{"id":123,"html_url":"https://github.com/acme/ext/releases/tag/untagged-0123"}' ;;
  https://uploads.github.com/repos/acme/ext/releases/*/assets?name=*)
    [[ "${SCENARIO:-}" != upload_failure && "${SCENARIO:-}" != force_upload_failure ]] || exit 1
    cp "$input" "$FIXTURE_ROOT/uploaded.zip"
    jq -n --arg name "${endpoint##*name=}" --argjson size "$(stat -c %s "$input")" '{id:9,name:$name,state:"uploaded",size:$size}' ;;
  repos/acme/ext/releases/123)
    [[ "${SCENARIO:-}" != publish_failure ]] || exit 1
    echo '{"draft":false,"html_url":"https://github.com/acme/ext/releases/tag/1.2.3-a1"}' ;;
  *) echo "Unexpected API endpoint: $endpoint" >&2; exit 1 ;;
esac
MOCK
chmod +x "$FIXTURE_ROOT/bin/git" "$FIXTURE_ROOT/bin/gh"
export PATH="$FIXTURE_ROOT/bin:$PATH"
script="$root/git-extensions.sh"
zip_file="$FIXTURE_ROOT/output/acme-ext-1.2.3-a1.zip"

run() {
  : >"$FIXTURE_ROOT/api.log"
  : >"$FIXTURE_ROOT/methods.log"
  if (($#)); then
    bash "$script" --remote "$@" >"$FIXTURE_ROOT/stdout" 2>"$FIXTURE_ROOT/stderr"
  else
    bash "$script" >"$FIXTURE_ROOT/stdout" 2>"$FIXTURE_ROOT/stderr"
  fi
}

run
grep -q '^Usage:' "$FIXTURE_ROOT/stdout"
[[ ! -s "$FIXTURE_ROOT/api.log" ]]
echo 'ok - no arguments show help without API calls'

if run --bogus; then exit 1; else [[ $? -eq 2 ]]; fi
if run --release --create; then exit 1; else [[ $? -eq 2 ]]; fi
echo 'ok - invalid and conflicting options fail'

run --release --dry-run --repo acme/ext --output "$FIXTURE_ROOT/output"
unzip -tq "$zip_file" >/dev/null
[[ "$(unzip -p "$zip_file" acme/ext/composer.json | jq -r .version)" == 1.2.3-a1 ]]
unzip -Z1 "$zip_file" | grep -q '^acme/ext/included.txt$'
if unzip -Z1 "$zip_file" | grep -q excluded.txt; then exit 1; fi
[[ -z "$(unzip -z "$zip_file" | tail -n +2)" ]]
grep -q 'Example merged change' "$FIXTURE_ROOT/stdout"
if grep -qE 'releases with-input|uploads.github.com|releases/123' "$FIXTURE_ROOT/api.log"; then exit 1; fi
[[ "$(git -C "$FIXTURE_ROOT/source" branch --show-current)" == main ]]
[[ "$(jq -r .version "$FIXTURE_ROOT/source/composer.json")" == 9.9.9 ]]
echo 'ok - dry run packages the pinned commit, honors exclusions, previews notes, and preserves the dirty local tree'

run --release --repo acme/ext --output "$FIXTURE_ROOT/output"
jq -e --arg sha "$FIXTURE_COMMIT" '.draft == true and .prerelease == true and .tag_name == "1.2.3-a1" and .target_commitish == $sha and (.body | contains("Example merged change"))' "$FIXTURE_ROOT/create.json" >/dev/null
cmp "$zip_file" "$FIXTURE_ROOT/uploaded.zip"
grep -q 'Published release: https://github.com/acme/ext/releases/tag/1.2.3-a1' "$FIXTURE_ROOT/stdout"
echo 'ok - release stages a prerelease draft, uploads the matching ZIP, and publishes with notes'

for scenario in existing_release existing_tag main_failure tag_failure; do
  export SCENARIO="$scenario"
  if run --release --repo acme/ext --output "$FIXTURE_ROOT/output"; then exit 1; fi
  if grep -q 'releases with-input' "$FIXTURE_ROOT/api.log"; then exit 1; fi
  echo "ok - $scenario stops before release creation"
done

export SCENARIO=archive_failure
before=$(sha256sum "$zip_file")
if run --release --repo acme/ext --output "$FIXTURE_ROOT/output"; then exit 1; fi
[[ "$before" == "$(sha256sum "$zip_file")" ]]
[[ -z "$(find "$FIXTURE_ROOT/output" -name '*.partial' -print)" ]]
echo 'ok - failed packaging preserves an existing ZIP and removes partial files'

for scenario in upload_failure publish_failure; do
  export SCENARIO="$scenario"
  if run --release --repo acme/ext --output "$FIXTURE_ROOT/output"; then exit 1; fi
  grep -q 'Release workflow incomplete. Inspect the release before retrying: https://github.com/acme/ext/releases/tag/untagged-0123' "$FIXTURE_ROOT/stderr"
  if [[ "$scenario" == upload_failure ]]; then
    if grep -q '^repos/acme/ext/releases/123 ' "$FIXTURE_ROOT/api.log"; then exit 1; fi
  fi
  echo "ok - $scenario reports the draft and never claims success"
done

export SCENARIO=master_branch
run --release --dry-run --repo acme/ext --output "$FIXTURE_ROOT/output"
grep -q '^GET repos/acme/ext/commits/master$' "$FIXTURE_ROOT/methods.log"
grep -q "Source: acme/ext master at $FIXTURE_COMMIT" "$FIXTURE_ROOT/stdout"
echo 'ok - a repository whose default branch is master is packaged from master'

export SCENARIO=archived
if run --release --dry-run --repo acme/ext --output "$FIXTURE_ROOT/output"; then exit 1; fi
grep -q 'acme/ext is archived' "$FIXTURE_ROOT/stderr"
[[ "$(cat "$FIXTURE_ROOT/methods.log")" == 'GET repos/acme/ext' ]]
echo 'ok - an archived repository is rejected before any other request'

dev_zip="$FIXTURE_ROOT/output/acme-ext-1.2.3-dev.zip"
export SCENARIO=dev_existing
run --release --dry-run --repo acme/ext --output "$FIXTURE_ROOT/output"
grep -q 'Would update rolling release 1.2.3-dev in place' "$FIXTURE_ROOT/stdout"
grep -q "Would move tag 1.2.3-dev from $FIXTURE_COMMIT to $DEV_COMMIT" "$FIXTURE_ROOT/stdout"
if grep -vE '^(GET |POST repos/acme/ext/releases/generate-notes$)' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
echo 'ok - dev dry run previews the rolling update without touching the tag, release, or asset'

run --release --repo acme/ext --output "$FIXTURE_ROOT/output"
[[ "$(unzip -p "$dev_zip" acme/ext/composer.json | jq -r .version)" == 1.2.3-dev ]]
cmp "$dev_zip" "$FIXTURE_ROOT/uploaded.zip"
jq -e --arg sha "$DEV_COMMIT" '.prerelease == true and .target_commitish == $sha and (has("draft") | not) and (.body | contains("Example merged change"))' "$FIXTURE_ROOT/update.json" >/dev/null
expected="PATCH repos/acme/ext/git/refs/tags/1.2.3-dev
POST repos/acme/ext/releases/generate-notes
PATCH repos/acme/ext/releases/456
GET repos/acme/ext/releases/456/assets?per_page=100
DELETE repos/acme/ext/releases/assets/8
POST https://uploads.github.com/repos/acme/ext/releases/456/assets?name=acme-ext-1.2.3-dev.zip.new
DELETE repos/acme/ext/releases/assets/7
PATCH repos/acme/ext/releases/assets/9
PATCH repos/acme/ext/releases/456"
# Notes are generated once for the preview and again after the tag moves.
[[ "$(grep -c generate-notes "$FIXTURE_ROOT/methods.log")" == 2 ]]
[[ "$(sed -n '/^PATCH repos\/acme\/ext\/git\/refs/,$p' "$FIXTURE_ROOT/methods.log")" == "$expected" ]]
if grep -qE '^POST repos/acme/ext/(releases|git/refs)$' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
grep -q 'Updated rolling release: https://github.com/acme/ext/releases/tag/1.2.3-dev' "$FIXTURE_ROOT/stdout"
if grep -q 'Published release' "$FIXTURE_ROOT/stdout"; then exit 1; fi
echo 'ok - dev release moves the tag, regenerates notes, and replaces the ZIP in the same release'

export SCENARIO=alpha_dev
run --release --dry-run --repo acme/ext --output "$FIXTURE_ROOT/output"
grep -q 'Release: 1.2.3-a2-dev (prerelease: true)' "$FIXTURE_ROOT/stdout"
[[ "$(unzip -p "$FIXTURE_ROOT/output/acme-ext-1.2.3-a2-dev.zip" acme/ext/composer.json | jq -r .version)" == 1.2.3-a2-dev ]]
echo 'ok - an alpha with -dev (1.2.3-a2-dev) is accepted as a rolling prerelease'

export SCENARIO=dev_new
run --release --repo acme/ext --output "$FIXTURE_ROOT/output"
jq -e --arg sha "$DEV_COMMIT" '.draft == true and .prerelease == true and .tag_name == "1.2.3-dev" and .target_commitish == $sha' "$FIXTURE_ROOT/create.json" >/dev/null
grep -q '^POST repos/acme/ext/git/refs$' "$FIXTURE_ROOT/methods.log"
grep -q 'Published release:' "$FIXTURE_ROOT/stdout"
echo 'ok - first dev release is created as a normal prerelease'
# ZIP-only remote creation must not inspect or mutate releases.
export SCENARIO=existing_release
run --create --repo acme/ext --output "$FIXTURE_ROOT/zip-only"
[[ -f "$FIXTURE_ROOT/zip-only/acme-ext-1.2.3-a1.zip" ]]
if grep -q '/releases' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
echo 'ok - remote ZIP creation does not require a new release version'

export SCENARIO=force_existing
if run --release --repo acme/ext --output "$FIXTURE_ROOT/output"; then exit 1; fi
run --release --force --dry-run --repo acme/ext --output "$FIXTURE_ROOT/output"
grep -q 'Would replace existing release 1.2.3 in place (--force)' "$FIXTURE_ROOT/stdout"
if grep -vE '^(GET |POST repos/acme/ext/releases/generate-notes$)' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
run --release --force --repo acme/ext --output "$FIXTURE_ROOT/output"
jq -e --arg sha "$STABLE_COMMIT" '.prerelease == false and .target_commitish == $sha' "$FIXTURE_ROOT/update.json" >/dev/null
grep -q '^PATCH repos/acme/ext/git/refs/tags/1.2.3$' "$FIXTURE_ROOT/methods.log"
grep -q 'Updated existing release:' "$FIXTURE_ROOT/stdout"
if grep -q '/assets/10' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
cmp "$FIXTURE_ROOT/output/acme-ext-1.2.3.zip" "$FIXTURE_ROOT/uploaded.zip"
echo 'ok - force replaces a stable annotated tag and ZIP, preserving stable status and unrelated assets'

for scenario in force_tag_only force_release_only; do
  export SCENARIO="$scenario"
  run --release --force --repo acme/ext --output "$FIXTURE_ROOT/output"
  grep -q 'release:' "$FIXTURE_ROOT/stdout"
  echo "ok - $scenario completes a forced replacement"
done

for scenario in force_tag_failure force_upload_failure force_publish_failure force_immutable; do
  export SCENARIO="$scenario"
  if run --release --force --repo acme/ext --output "$FIXTURE_ROOT/output"; then exit 1; fi
  if grep -qE '^(Updated|Published) .*release:' "$FIXTURE_ROOT/stdout"; then exit 1; fi
  if [[ "$scenario" == force_upload_failure ]]; then
    if grep -q '^DELETE repos/acme/ext/releases/assets/7$' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
  fi
  if [[ "$scenario" == force_immutable ]]; then
    if grep -qE '^(PATCH|DELETE|POST) ' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
  fi
  echo "ok - $scenario fails without claiming success or overriding server protections"
done

# --gh-release from a local checkout, through the same shared release logic.
unset SCENARIO
git clone -q "$FIXTURE_ROOT/source" "$FIXTURE_ROOT/local"
cd "$FIXTURE_ROOT/local"
local_zip="$FIXTURE_ROOT/git-exported/acme-ext-1.2.3-a1.zip"

run_local() {
  : >"$FIXTURE_ROOT/api.log"
  : >"$FIXTURE_ROOT/methods.log"
  bash "$root/$1" "${@:2}" >"$FIXTURE_ROOT/stdout" 2>"$FIXTURE_ROOT/stderr"
}

run_local git-extensions.sh --gh-release --dry-run
unzip -tq "$local_zip" >/dev/null
grep -q 'Dry run: no GitHub changes made.' "$FIXTURE_ROOT/stdout"
if grep -vE '^(GET |POST repos/acme/ext/releases/generate-notes$)' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
echo 'ok - git-extensions.sh --gh-release --dry-run builds the ZIP and makes no GitHub changes'

run_local git-extensions.sh --gh-release
jq -e --arg sha "$FIXTURE_COMMIT" '.prerelease == true and .tag_name == "1.2.3-a1" and .target_commitish == $sha' "$FIXTURE_ROOT/create.json" >/dev/null
cmp "$local_zip" "$FIXTURE_ROOT/uploaded.zip"
grep -q 'Published release: https://github.com/acme/ext/releases/tag/1.2.3-a1' "$FIXTURE_ROOT/stdout"
echo 'ok - git-extensions.sh --gh-release publishes local HEAD as a prerelease'

export SCENARIO=existing_release
if run_local git-extensions.sh --gh-release; then exit 1; fi
grep -q 'Release 1.2.3-a1 already exists' "$FIXTURE_ROOT/stderr"
echo 'ok - git-extensions.sh --gh-release rejects an existing non-dev release'

unset SCENARIO
echo dirty >>included.txt
if run_local git-extensions.sh --gh-release; then exit 1; fi
grep -q 'uncommitted changes' "$FIXTURE_ROOT/stderr"
[[ ! -s "$FIXTURE_ROOT/methods.log" ]]
git checkout -q -- included.txt
echo 'ok - git-extensions.sh --gh-release refuses uncommitted changes before contacting GitHub'

git -c user.name=test -c user.email=test@example.com commit -q --allow-empty -m unpushed
if run_local git-extensions.sh --gh-release; then exit 1; fi
grep -q 'is not on GitHub; push it before releasing' "$FIXTURE_ROOT/stderr"
git reset -q --hard origin/main
echo 'ok - git-extensions.sh --gh-release refuses a commit that is not pushed'

git checkout -q dev
export SCENARIO=dev_existing
run_local git-extensions.sh --gh-release
grep -q '^PATCH repos/acme/ext/git/refs/tags/1.2.3-dev$' "$FIXTURE_ROOT/methods.log"
grep -q 'Updated rolling release: https://github.com/acme/ext/releases/tag/1.2.3-dev' "$FIXTURE_ROOT/stdout"
cmp "$FIXTURE_ROOT/git-exported/acme-ext-1.2.3-dev.zip" "$FIXTURE_ROOT/uploaded.zip"
echo 'ok - git-extensions.sh --gh-release updates the rolling -dev release in place'

# A style and a language pack, committed into the fixture so their commits count as pushed.
unset SCENARIO
git -C "$FIXTURE_ROOT/source" checkout -q --orphan style
git -C "$FIXTURE_ROOT/source" rm -rqf --cached .
printf 'name = AcmeStyle\nstyle_version = 1.2.3-a1\n' >"$FIXTURE_ROOT/source/style.cfg"
git -C "$FIXTURE_ROOT/source" add style.cfg
git -C "$FIXTURE_ROOT/source" -c user.name=test -c user.email=test@example.com commit -qm style
git clone -q -b style "$FIXTURE_ROOT/source" "$FIXTURE_ROOT/style"
cd "$FIXTURE_ROOT/style"
run_local git-release-style.sh --gh-release
jq -e '.prerelease == true and .tag_name == "1.2.3-a1"' "$FIXTURE_ROOT/create.json" >/dev/null
unzip -Z1 "$FIXTURE_ROOT/uploaded.zip" | grep -q "^AcmeStyle/style.cfg$"
grep -q 'Published release:' "$FIXTURE_ROOT/stdout"
echo 'ok - git-release-style.sh --gh-release publishes a prerelease'
REMOTE_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse style)
export REMOTE_COMMIT
run_local git-release-style.sh --create --remote --repo acme/ext --output "$FIXTURE_ROOT/remote-style"
unzip -Z1 "$FIXTURE_ROOT/remote-style/AcmeStyle_1.2.3-a1.zip" | grep -q '^AcmeStyle/style.cfg$'
export SCENARIO=force_existing FORCE_VERSION=1.2.3-a1 FORCE_ASSET=AcmeStyle_1.2.3-a1.zip
run_local git-release-style.sh --release --remote --force
jq -e '.prerelease == true' "$FIXTURE_ROOT/update.json" >/dev/null
grep -q 'Updated existing release:' "$FIXTURE_ROOT/stdout"
echo 'ok - styles support remote ZIP creation and forced prerelease replacement'
unset SCENARIO REMOTE_COMMIT FORCE_VERSION FORCE_ASSET


git -C "$FIXTURE_ROOT/source" checkout -q --orphan langpack
git -C "$FIXTURE_ROOT/source" rm -rqf --cached .
mkdir -p "$FIXTURE_ROOT/source/language/xx"
printf 'Example Language\nExample\n' >"$FIXTURE_ROOT/source/language/xx/iso.txt"
git -C "$FIXTURE_ROOT/source" add language
git -C "$FIXTURE_ROOT/source" -c user.name=test -c user.email=test@example.com commit -qm langpack
git clone -q -b langpack "$FIXTURE_ROOT/source" "$FIXTURE_ROOT/langpack"
cd "$FIXTURE_ROOT/langpack"
run_local git-release-langpack.sh --gh-release --version 1.2.3-a1
jq -e '.prerelease == true and .tag_name == "1.2.3-a1"' "$FIXTURE_ROOT/create.json" >/dev/null
unzip -Z1 "$FIXTURE_ROOT/uploaded.zip" | grep -q '^example_language_1.2.3-a1/language/xx/iso.txt$'
grep -q 'Published release:' "$FIXTURE_ROOT/stdout"
echo 'ok - git-release-langpack.sh --gh-release publishes the pack'
REMOTE_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse langpack)
export REMOTE_COMMIT
run_local git-release-langpack.sh --create --remote --repo acme/ext --version 1.2.3-a1 --output "$FIXTURE_ROOT/remote-language"
unzip -Z1 "$FIXTURE_ROOT/remote-language/example_language_1.2.3-a1.zip" | grep -q '^example_language_1.2.3-a1/language/xx/iso.txt$'
export SCENARIO=force_existing FORCE_VERSION=1.2.3-a1 FORCE_ASSET=example_language_1.2.3-a1.zip
run_local git-release-langpack.sh --release --remote --force --version 1.2.3-a1
jq -e '.prerelease == true' "$FIXTURE_ROOT/update.json" >/dev/null
grep -q 'Updated existing release:' "$FIXTURE_ROOT/stdout"
echo 'ok - language packs support remote ZIP creation and forced replacement'
unset SCENARIO REMOTE_COMMIT FORCE_VERSION FORCE_ASSET


# Reuse cached reference/validator fixtures to test the remote validation gate
# without downloads, dependencies, or execution of language-pack PHP files.
export XDG_CACHE_HOME="$FIXTURE_ROOT/cache"
reference="$XDG_CACHE_HOME/git-release-langpack/phpbb-3.3.17/en/language/en"
validator="$XDG_CACHE_HOME/git-release-langpack/translation-validator-1.6.x"
mkdir -p "$reference" "$validator/vendor"
: >"$validator/vendor/autoload.php"
echo reference >"$reference/common.php"
cat >"$FIXTURE_ROOT/bin/php" <<'MOCKPHP'
#!/usr/bin/bash
set -eu
stage=''
for argument in "$@"; do
  if [[ "$argument" == --package-dir=* ]]; then stage="${argument#*=}"; fi
done
[[ -f "$stage/xx/language/xx/iso.txt" && -f "$stage/en/language/en/common.php" ]]
grep -q '^Example Language$' "$stage/xx/language/xx/iso.txt"
if [[ "${VALIDATOR_FAIL:-0}" == 1 ]]; then
  echo 'Fatal: 0, Error: 1'
  exit 1
fi
echo 'Fatal: 0, Error: 0'
MOCKPHP
cat >"$FIXTURE_ROOT/bin/composer" <<'MOCKCOMPOSER'
#!/usr/bin/bash
# The cached validator must avoid invoking Composer.
exit 1
MOCKCOMPOSER
chmod +x "$FIXTURE_ROOT/bin/php" "$FIXTURE_ROOT/bin/composer"
REMOTE_COMMIT=$(git -C "$FIXTURE_ROOT/source" rev-parse langpack)
export REMOTE_COMMIT
run_local git-release-langpack.sh --create --remote --check --version 3.3.17 --output "$FIXTURE_ROOT/validated"
grep -q 'Validation passed.' "$FIXTURE_ROOT/stdout"
validated_zip="$FIXTURE_ROOT/validated/example_language_3.3.17.zip"
unzip -Z1 "$validated_zip" | grep -q '^example_language_3.3.17/language/xx/iso.txt$'
# A validation-only run does not save a ZIP or inspect releases.
run_local git-release-langpack.sh --remote --check --version 3.3.17 --output "$FIXTURE_ROOT/check-only"
[[ ! -e "$FIXTURE_ROOT/check-only" ]]
if grep -q '/releases' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
export SCENARIO=force_existing FORCE_VERSION=3.3.17 FORCE_ASSET=example_language_3.3.17.zip VALIDATOR_FAIL=1
before=$(sha256sum "$validated_zip")
if run_local git-release-langpack.sh --release --remote --force --check --version 3.3.17 --output "$FIXTURE_ROOT/validated"; then exit 1; fi
grep -q 'not releasing: the pack does not pass validation' "$FIXTURE_ROOT/stderr"
[[ "$before" == "$(sha256sum "$validated_zip")" ]]
if grep -qE '^(POST|PATCH|DELETE) ' "$FIXTURE_ROOT/methods.log"; then exit 1; fi
echo 'ok - remote language validation uses the packaged snapshot; failure preserves the ZIP and blocks even forced publishing'
unset SCENARIO REMOTE_COMMIT FORCE_VERSION FORCE_ASSET VALIDATOR_FAIL

echo 'All API release tests passed.'
