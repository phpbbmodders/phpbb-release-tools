#!/usr/bin/bash
# Build a phpBB language pack ZIP from local HEAD or the remote default branch.
# --create builds locally; --release publishes to GitHub; --dry-run previews it.
# --force permits replacing an existing tag/release, subject to GitHub policy.
# Requires the adjacent lib/ helpers; symlinking this entry point is supported.
# Use --help for package-specific options and requirements in README.md.
# Exit status: 0 success, 1 operational failure, 2 invalid usage.

set -Eeuo pipefail

readonly VERSION_FEED_URL="https://version.phpbb.com/phpbb/versions.json"
readonly RELEASE_BASE_URL="https://download.phpbb.com/pub/release"
readonly VALIDATOR_REPO_URL="https://github.com/phpbb/phpbb-translation-validator"
# The validator's master branch only supports phpBB 4.0; 1.6.x supports 3.3.
readonly VALIDATOR_BRANCH_33="1.6.x"
# Top-level folders that make up a language pack package.
readonly PACK_DIRS=(ext language styles)

readonly CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/git-release-langpack"

# Shared release logic lives next to the real script, even when it is run through a symlink.
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
readonly SCRIPT_DIR
# shellcheck source=lib/github-release.sh
source "$SCRIPT_DIR/lib/github-release.sh"
# shellcheck source=lib/release-source.sh
source "$SCRIPT_DIR/lib/release-source.sh"

mode=""
dry_run=0
do_check=0
GHR_SCRATCH=""
opt_version=""
opt_line=""

work_dir=""

usage() {
  release_usage 'git-release-langpack.sh'
  cat <<'HELP'

Language-pack options:
  -c, --check            Validate the exact ZIP contents (phpBB 3.3 only).
                         Can run alone, or with --create/--release.
  -v, --version VERSION  phpBB version to name and validate against.
  -l, --line LINE        Release line for version lookup (default 3.3).

Version defaults to the current stable release from phpBB's version feed.
Only ext/, language/, and styles/ are packaged, honoring export-ignore.
Validation must pass before a ZIP is saved or anything is published.
Official reference ZIPs are SHA-256 checked. References and the validator are
cached under $XDG_CACHE_HOME/git-release-langpack (default ~/.cache/git-release-langpack).

Examples:
  git-release-langpack.sh --check
  git-release-langpack.sh --create --check --remote
  git-release-langpack.sh --release --remote --check --dry-run
HELP
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

cleanup() {
  if [[ -n "$work_dir" && -d "$work_dir" ]]; then
    rm -rf -- "$work_dir"
  fi
  [[ -z "$GHR_SCRATCH" ]] || rm -rf -- "$GHR_SCRATCH"
  ghr_report_incomplete
}

# Fail early with a clear message when a required program is missing.
require_commands() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
  done
}

parse_args() {
  if [[ $# -eq 0 ]]; then
    usage
    exit 0
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -C|--create|-r|--release|-g|--gh-release)
        [[ -z "$mode" ]] || usage_error "only one of --create or --release may be used"
        case "$1" in -C|--create) mode="create" ;; *) mode="gh-release" ;; esac
        ;;
      --remote) RELEASE_REMOTE=true ;;
      -R|--repo)
        release_option_value "$@"
        RELEASE_REPO="$2"
        shift
        ;;
      -f|--force) GHR_FORCE=true ;;
      -c|--check) do_check=1 ;;
      -n|--dry-run) dry_run=1 ;;
      -v|--version)
        release_option_value "$@"
        opt_version="$2"
        shift
        ;;
      -l|--line)
        release_option_value "$@"
        opt_line="$2"
        shift
        ;;
      -o|--output|--output-dir)
        release_option_value "$@"
        RELEASE_OUTPUT="$2"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *) usage_error "unknown option: $1" ;;
    esac
    shift
  done

  if [[ -z "$mode" && $do_check -eq 0 ]]; then
    usage_error "choose an operation: --create, --release, or --check"
  fi
  if [[ $dry_run -eq 1 && "$mode" != "gh-release" ]]; then
    usage_error "--dry-run requires --release"
  fi
  if [[ -n "$opt_version" && ! "$opt_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.-]*$ ]]; then
    usage_error "--version must look like 3.3.17, got: $opt_version"
  fi
  if [[ -n "$opt_line" && ! "$opt_line" =~ ^[0-9]+\.[0-9]+$ ]]; then
    usage_error "--line must look like 3.3, got: $opt_line"
  fi
}

# Print the phpBB release line to use: --line, else derived from --version,
# else 3.3.
resolve_line() {
  if [[ -n "$opt_line" ]]; then
    echo "$opt_line"
  elif [[ -n "$opt_version" ]]; then
    echo "${opt_version%.*}"
  else
    echo "3.3"
  fi
}

# Print the current stable phpBB version of a release line from the version
# feed. Fails loudly rather than guessing, because the result becomes part of
# a published file name.
fetch_stable_version() {
  local line="$1" feed version
  feed="$(curl -fsS --max-time 20 --retry 2 "$VERSION_FEED_URL")" \
    || die "could not fetch $VERSION_FEED_URL; pass --version to set it manually"
  version="$(jq -er --arg line "$line" '.stable[$line].current' <<<"$feed")" \
    || die "the version feed has no stable release for line $line; pass --version"
  if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.-]*$ ]]; then
    die "unexpected version in feed: $version; pass --version"
  fi
  echo "$version"
}

# Print the language pack's ISO code (the language/<iso> directory name).
# Requires exactly one committed language/<iso>/iso.txt.
detect_iso() {
  local -a iso_files
  mapfile -t iso_files < <(git ls-tree -r --name-only "$RELEASE_COMMIT" -- language \
    | grep -E '^language/[^/]+/iso\.txt$' || true)
  [[ ${#iso_files[@]} -eq 1 ]] \
    || die "expected exactly one committed language/<iso>/iso.txt, found ${#iso_files[@]}"
  iso_files[0]="${iso_files[0]#language/}"
  echo "${iso_files[0]%%/*}"
}

# Print the filesystem-safe package name for an ISO code: line 1 of iso.txt
# (English language name), lowercased, with spaces and hyphens as underscores.
package_name() {
  local iso="$1" english_name slug
  english_name="$(git show "$RELEASE_COMMIT:language/$iso/iso.txt" | head -n 1 | tr -d '\r')"
  slug="$(tr '[:upper:]' '[:lower:]' <<<"$english_name" \
    | tr ' -' '__' | tr -cd 'a-z0-9_')"
  [[ -n "$slug" ]] || die "language/$iso/iso.txt line 1 gives an empty package name"
  echo "$slug"
}

# Build the release zip at $1 with top-level folder $2 from the committed
# HEAD tree. Only the folders that make up a language pack are included.
build_zip() {
  local zip_file="$1" folder="$2" dir
  local -a present=()
  for dir in "${PACK_DIRS[@]}"; do
    if git ls-tree --name-only "$RELEASE_COMMIT" -- "$dir" | grep -qx "$dir"; then
      present+=("$dir")
    else
      echo "Warning: $dir/ is not in the repository; the pack will not contain it." >&2
    fi
  done
  [[ ${#present[@]} -gt 0 ]] || die "none of ${PACK_DIRS[*]} found in the repository"

  git archive --format=zip -9 --prefix="$folder/" --output="$zip_file" "$RELEASE_COMMIT" -- "${present[@]}"

  # git archive stores the commit hash as the zip comment; clear it.
  zip -q -z "$zip_file" </dev/null
}

# Make sure the official English reference files for phpBB $1 are cached, and
# print their directory. The release zip is checked against its published
# SHA-256 before use.
ensure_reference() {
  local version="$1" line="$2"
  local ref_dir="$CACHE_DIR/phpbb-$version"
  if [[ -d "$ref_dir/en/language/en" ]]; then
    echo "$ref_dir"
    return
  fi

  local url="$RELEASE_BASE_URL/$line/$version/phpBB-$version.zip"
  local zip="$work_dir/phpbb-release.zip" expected actual
  echo "Downloading phpBB $version reference files ($url)" >&2
  curl -fsSL --max-time 300 --retry 2 -o "$zip" "$url" \
    || die "could not download $url (does phpBB $version exist?)"
  expected="$(curl -fsSL --max-time 30 --retry 2 "$url.sha256" | cut -d' ' -f1)" \
    || die "could not download $url.sha256"
  actual="$(sha256sum "$zip" | cut -d' ' -f1)"
  [[ -n "$expected" && "$expected" == "$actual" ]] \
    || die "SHA-256 mismatch for phpBB-$version.zip (expected '$expected', got '$actual')"

  local extract="$work_dir/reference-extract" staged="$work_dir/reference-staged"
  unzip -q "$zip" \
    'phpBB3/language/en/*' \
    'phpBB3/ext/phpbb/viglink/language/en/*' \
    'phpBB3/styles/prosilver/theme/en/*' \
    -d "$extract"
  mkdir -p "$staged/en/language" \
    "$staged/en/ext/phpbb/viglink/language" \
    "$staged/en/styles/prosilver/theme"
  mv "$extract/phpBB3/language/en" "$staged/en/language/en"
  mv "$extract/phpBB3/ext/phpbb/viglink/language/en" "$staged/en/ext/phpbb/viglink/language/en"
  mv "$extract/phpBB3/styles/prosilver/theme/en" "$staged/en/styles/prosilver/theme/en"

  mkdir -p "$CACHE_DIR"
  mv "$staged" "$ref_dir"
  echo "$ref_dir"
}

# Make sure the validator is cloned and installed, and print its directory.
ensure_validator() {
  local line="$1" branch
  case "$line" in
    3.3) branch="$VALIDATOR_BRANCH_33" ;;
    *) die "validation is only supported for the 3.3 line, not $line" ;;
  esac

  local validator_dir="$CACHE_DIR/translation-validator-$branch"
  if [[ -f "$validator_dir/vendor/autoload.php" ]]; then
    echo "$validator_dir"
    return
  fi

  echo "Installing the phpBB Translation Validator ($branch)" >&2
  local log="$work_dir/validator-install.log"
  rm -rf -- "$validator_dir"
  mkdir -p "$CACHE_DIR"
  git clone -q --depth 1 --branch "$branch" "$VALIDATOR_REPO_URL" "$validator_dir" \
    || die "could not clone $VALIDATOR_REPO_URL"
  if ! (cd "$validator_dir" && composer install -q --no-dev --no-interaction >"$log" 2>&1); then
    cat "$log" >&2
    rm -rf -- "$validator_dir"
    die "composer install failed for the validator"
  fi
  echo "$validator_dir"
}

# Validate the unpacked release folder $1 (ISO code $2) against the official
# English files for phpBB $3 (release line $4). Returns non-zero on any fatal
# or error-level finding; warnings and notices do not fail the check.
run_validation() {
  local unpacked="$1" iso="$2" version="$3" line="$4"
  local ref_dir validator_dir stage="$work_dir/stage"

  ref_dir="$(ensure_reference "$version" "$line")" || return 1
  validator_dir="$(ensure_validator "$line")" || return 1

  mkdir -p "$stage"
  cp -r "$ref_dir/en" "$stage/en"
  mv "$unpacked" "$stage/$iso"

  echo "Validating '$iso' against phpBB $version"
  local report status=0
  # Safe mode parses the pack's PHP files instead of including them.
  report="$(php -d error_reporting='E_ALL & ~E_DEPRECATED' \
    "$validator_dir/translation.php" validate "$iso" \
    --package-dir="$stage" --phpbb-version="$line" --safe-mode 2>&1)" || status=$?
  echo "$report"

  # A missing summary is a failure, not a pass.
  local fatal errors
  if [[ ! "$report" =~ Fatal:\ ([0-9]+),\ Error:\ ([0-9]+) ]]; then
    echo "Validation failed: could not read the validator summary." >&2
    return 1
  fi
  fatal="${BASH_REMATCH[1]}"
  errors="${BASH_REMATCH[2]}"
  if [[ $status -ne 0 || $fatal -gt 0 || $errors -gt 0 ]]; then
    echo "Validation failed: $fatal fatal, $errors error." >&2
    return 1
  fi
  echo "Validation passed."
}

main() {
  parse_args "$@"
  release_validate_options "$mode"

  require_commands git curl jq unzip zip
  if [[ $do_check -eq 1 ]]; then
    require_commands php composer sha256sum
  fi
  if [[ "$mode" == "gh-release" ]]; then
    require_commands gh timeout
    gh auth status >/dev/null 2>&1 || die "GitHub CLI is not authenticated; run: gh auth login"
  fi

  work_dir="$(mktemp -d)"
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  release_prepare_source "$mode"
  local commit="$RELEASE_COMMIT"

  local line version iso name folder
  line="$(resolve_line)"
  if [[ -n "$opt_version" ]]; then
    version="$opt_version"
  else
    version="$(fetch_stable_version "$line")"
    echo "Using current stable phpBB $line release: $version"
  fi
  iso="$(detect_iso)"
  name="$(package_name "$iso")"
  folder="${name}_${version}"

  # Check GitHub before building: the repository, the pushed commit, and any
  # existing release or tag for this version.
  if [[ "$mode" == "gh-release" ]]; then
    if [[ -z "$GHR_SCRATCH" ]]; then GHR_SCRATCH="$(mktemp -d)"; fi
    if [[ -z "$GHR_REPOSITORY" ]]; then ghr_resolve_repository ""; fi
    if [[ "$RELEASE_REMOTE" == false ]]; then ghr_require_remote_commit "$commit"; fi
    ghr_check_existing "$version"
  fi

  local zip_file="$work_dir/$folder.zip"
  build_zip "$zip_file" "$folder"

  if [[ $do_check -eq 1 ]]; then
    local unpack="$work_dir/unpack"
    mkdir -p "$unpack"
    unzip -q "$zip_file" -d "$unpack"
    run_validation "$unpack/$folder" "$iso" "$version" "$line" \
      || die "not releasing: the pack does not pass validation"
  fi

  if [[ -n "$mode" ]]; then
    local out_dir="$RELEASE_OUTPUT"
    mkdir -p "$out_dir"
    out_dir="$(cd "$out_dir" && pwd)"
    mv "$zip_file" "$out_dir/$folder.zip.partial"
    mv "$out_dir/$folder.zip.partial" "$out_dir/$folder.zip"
    echo "Release $folder.zip created at $out_dir/$folder.zip"
  fi

  if [[ "$mode" == "gh-release" ]]; then
    echo
    echo "GitHub release:"
    printf '  Repository:  %s\n' "$GHR_REPOSITORY"
    printf '  Tag:         %s\n' "$version"
    printf '  Commit:      %s\n' "$commit"
    printf '  Asset:       %s\n' "$folder.zip"
    echo
    ghr_generate_notes "$version" "$commit"
    if [[ $dry_run -eq 1 ]]; then
      ghr_preview "$version" "$commit"
      echo
      echo "Dry run: no GitHub changes made."
      GHR_COMPLETED=true
      return
    fi
    ghr_publish "$version" "$commit" "$out_dir/$folder.zip"
  fi
}

main "$@"
