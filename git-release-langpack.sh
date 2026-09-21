#!/usr/bin/bash
# git-release-langpack.sh
#
# Builds a phpBB.com Customisation Database release zip for a phpBB language
# pack from the current git commit, and optionally validates it first.
#
# The zip follows the Language Pack Validation Policy naming rule:
#   <languagename>_<version>.zip containing a <languagename>_<version>/ folder,
# and holds only ext/, language/ and styles/ (so repository files such as the
# root README.md and SECURITY.md are left out). Files marked export-ignore in
# .gitattributes are also left out.
#
# Inputs (run from anywhere inside the language pack's git repository):
#   The committed tree of HEAD. Uncommitted changes are not included.
#   language/<iso>/iso.txt supplies the English language name (line 1).
#   The phpBB version defaults to the current stable release of the chosen
#   line, read from phpBB's update-check feed; -v overrides it.
#
# Outputs:
#   <output-dir>/<languagename>_<version>.zip  (with --release)
#   A validator report on stdout                (with --check)
#
# Exit status: 0 on success, 1 on failure (including failed validation),
# 2 on invalid usage.
#
# Requirements: git, curl, jq, unzip, zip. For --check also: php, composer.

set -Eeuo pipefail

readonly VERSION_FEED_URL="https://version.phpbb.com/phpbb/versions.json"
readonly RELEASE_BASE_URL="https://download.phpbb.com/pub/release"
readonly VALIDATOR_REPO_URL="https://github.com/phpbb/phpbb-translation-validator"
# The validator's master branch only supports phpBB 4.0; 1.6.x supports 3.3.
readonly VALIDATOR_BRANCH_33="1.6.x"
# Top-level folders that make up a language pack package.
readonly PACK_DIRS=(ext language styles)

readonly CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/git-release-langpack"

do_release=0
do_check=0
opt_version=""
opt_line=""
opt_output_dir=""

work_dir=""

usage() {
  cat <<'EOF'
Usage: git-release-langpack.sh [options]

Build a phpBB language pack release zip from the current git commit.
At least one of --release or --check is required.

Operations:
  -r, --release          Build <languagename>_<version>.zip in the output
                         directory.
  -c, --check            Validate the pack with the phpBB Translation Validator
                         (3.3 line only). Combined with --release, the zip is
                         only written if validation passes.

Options:
  -v, --version VERSION  phpBB version to name and validate against, for
                         example 3.3.17. Default: current stable release of
                         the line, from phpBB's version feed.
  -l, --line LINE        phpBB release line used to look up the stable
                         version, for example 3.3. Default: derived from
                         --version if given, otherwise 3.3.
  -o, --output-dir DIR   Directory for the zip. Default: ../git-exported
                         relative to the repository root.
  -h, --help             Show this help and exit.

Validation reference files (the official phpBB release zip, verified against
its published SHA-256) and the validator are cached in
$XDG_CACHE_HOME/git-release-langpack (default ~/.cache/git-release-langpack).
Delete that directory to force a fresh download.

Examples:
  git-release-langpack.sh --check
  git-release-langpack.sh --check --release
  git-release-langpack.sh --release --version 3.3.16
EOF
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
      -r|--release) do_release=1 ;;
      -c|--check) do_check=1 ;;
      -v|--version)
        [[ $# -ge 2 ]] || usage_error "$1 needs a value"
        opt_version="$2"
        shift
        ;;
      -l|--line)
        [[ $# -ge 2 ]] || usage_error "$1 needs a value"
        opt_line="$2"
        shift
        ;;
      -o|--output-dir)
        [[ $# -ge 2 ]] || usage_error "$1 needs a value"
        opt_output_dir="$2"
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

  if [[ $do_release -eq 0 && $do_check -eq 0 ]]; then
    usage_error "choose an operation: --release and/or --check"
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
  mapfile -t iso_files < <(git ls-tree -r --name-only HEAD -- language \
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
  english_name="$(git show "HEAD:language/$iso/iso.txt" | head -n 1 | tr -d '\r')"
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
    if git ls-tree --name-only HEAD -- "$dir" | grep -qx "$dir"; then
      present+=("$dir")
    else
      echo "Warning: $dir/ is not in the repository; the pack will not contain it." >&2
    fi
  done
  [[ ${#present[@]} -gt 0 ]] || die "none of ${PACK_DIRS[*]} found in the repository"

  git archive --format=zip -9 --prefix="$folder/" --output="$zip_file" HEAD -- "${present[@]}"

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

  require_commands git curl jq unzip zip
  if [[ $do_check -eq 1 ]]; then
    require_commands php composer sha256sum
  fi

  local repo_root
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || die "the current directory is not inside a git repository"
  cd "$repo_root"
  git rev-parse --verify -q HEAD >/dev/null || die "the repository has no commits"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Warning: uncommitted changes are not included; only HEAD is packaged." >&2
  fi

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

  work_dir="$(mktemp -d)"
  trap cleanup EXIT

  local zip_file="$work_dir/$folder.zip"
  build_zip "$zip_file" "$folder"

  if [[ $do_check -eq 1 ]]; then
    local unpack="$work_dir/unpack"
    mkdir -p "$unpack"
    unzip -q "$zip_file" -d "$unpack"
    run_validation "$unpack/$folder" "$iso" "$version" "$line" \
      || die "not releasing: the pack does not pass validation"
  fi

  if [[ $do_release -eq 1 ]]; then
    local out_dir="${opt_output_dir:-$repo_root/../git-exported}"
    mkdir -p "$out_dir"
    out_dir="$(cd "$out_dir" && pwd)"
    mv "$zip_file" "$out_dir/$folder.zip.partial"
    mv "$out_dir/$folder.zip.partial" "$out_dir/$folder.zip"
    echo "Release $folder.zip created at $out_dir/$folder.zip"
  fi
}

main "$@"
