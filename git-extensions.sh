#!/usr/bin/bash
# Build a phpBB extension ZIP from local HEAD or the remote default branch.
# --create builds locally; --release publishes to GitHub; --dry-run previews it.
# --force permits replacing an existing tag/release, subject to GitHub policy.
# Requires the adjacent lib/ helpers; symlinking this entry point is supported.
# Use --help for package-specific options and requirements in README.md.
# Exit status: 0 success, 1 operational failure, 2 invalid usage.

set -Eeuo pipefail

# Shared release logic lives next to the real script, even when it is run through a symlink.
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
readonly SCRIPT_DIR
# shellcheck source=lib/github-release.sh
source "$SCRIPT_DIR/lib/github-release.sh"
# shellcheck source=lib/release-source.sh
source "$SCRIPT_DIR/lib/release-source.sh"

MODE=""
DRY_RUN=false
ARCHIVE=""
VENDOR=""
RELEASE_NAME=""
VERSION=""
COMMIT_HASH=""
PARTIAL=""
GHR_SCRATCH=""

die() {
  echo "Error: $*" >&2
  exit 1
}

usage_error() {
  echo "Error: $*" >&2
  echo "Run with --help for usage." >&2
  exit 2
}

# Remove partial output and GitHub scratch files, and name any release a failed
# run left behind.
cleanup() {
  [[ -z "$PARTIAL" ]] || rm -f -- "$PARTIAL"
  [[ -z "$GHR_SCRATCH" ]] || rm -rf -- "$GHR_SCRATCH"
  ghr_report_incomplete
}

usage() {
  release_usage 'git-extensions.sh'
  cat <<'HELP'

The name (vendor/name) and version in composer.json name the ZIP and tag.
The ZIP contains vendor/name/. Files marked export-ignore are excluded.
HELP
}

require_commands() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
  done
}

parse_args() {
  if (($# == 0)); then
    usage
    exit 0
  fi

  while (($#)); do
    case "$1" in
      -C|--create)
        [[ -z "$MODE" ]] || usage_error "only one of --create or --release may be used"
        MODE="create"
        ;;
      -r|--release|-g|--gh-release)
        [[ -z "$MODE" ]] || usage_error "only one of --create or --release may be used"
        MODE="gh-release"
        ;;
      --remote) RELEASE_REMOTE=true ;;
      -R|--repo)
        release_option_value "$@"
        RELEASE_REPO="$2"
        shift
        ;;
      -o|--output|--output-dir)
        release_option_value "$@"
        RELEASE_OUTPUT="$2"
        shift
        ;;
      -f|--force) GHR_FORCE=true ;;

      -n|--dry-run)
        DRY_RUN=true
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        usage_error "unknown option: $1"
        ;;
    esac
    shift
  done

  [[ -n "$MODE" ]] || usage_error "one of --create or --release is required"
  if [[ "$DRY_RUN" == true && "$MODE" != "gh-release" ]]; then
    usage_error "--dry-run requires --release"
  fi
}

# Work from the repository root, so the script also works from a subdirectory.
prepare_repository() {
  release_prepare_source "$MODE"
  COMMIT_HASH="$RELEASE_COMMIT"
}

# Read vendor, name, and version from the committed composer.json (the same commit that is archived).
read_composer_metadata() {
  local composer_json package
  composer_json="$(git show "$COMMIT_HASH":composer.json 2>/dev/null)" \
    || die "composer.json is not committed in the current commit"
  package="$(jq -r '.name // empty' <<<"$composer_json")"
  VERSION="$(jq -r '.version // empty' <<<"$composer_json")"
  [[ -n "$package" && -n "$VERSION" ]] \
    || die "composer.json needs a \"name\" (vendor/name) and a \"version\""
  [[ "$package" =~ ^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$ ]] \
    || die "composer.json needs a safe vendor/name package name, got: $package"
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.-]*$ ]] \
    || die "invalid version in composer.json: $VERSION"
  VENDOR="${package%%/*}"
  RELEASE_NAME="${package#*/}"
}

# Create the archive in a temporary file, so a failure never replaces an existing ZIP.
create_archive() {
  local filename output_dir
  filename="$VENDOR-$RELEASE_NAME-$VERSION.zip"

  mkdir -p "$RELEASE_OUTPUT" || die "failed to create $RELEASE_OUTPUT"
  output_dir="$(cd "$RELEASE_OUTPUT" && pwd)" || die "failed to resolve $RELEASE_OUTPUT"

  ARCHIVE="$output_dir/$filename"
  PARTIAL="$output_dir/.$filename.partial"

  git archive --format=zip -9 --prefix="$VENDOR/$RELEASE_NAME/" --output="$PARTIAL" "$COMMIT_HASH" \
    || die "git archive failed"

  # git archive stores the commit hash as the ZIP archive comment.
  zip -q -z "$PARTIAL" </dev/null || die "failed to clear the ZIP comment"

  mv -- "$PARTIAL" "$ARCHIVE" || die "failed to write $ARCHIVE"
  PARTIAL=""

  echo "Release archive created:"
  echo "  $ARCHIVE"
}

# Check GitHub before building: the repository, the pushed commit, and any
# existing release or tag for this version.
prepare_github() {
  require_commands gh timeout
  gh auth status >/dev/null 2>&1 || die "GitHub CLI is not authenticated; run: gh auth login"

  if [[ -z "$GHR_SCRATCH" ]]; then GHR_SCRATCH="$(mktemp -d)"; fi
  if [[ -z "$GHR_REPOSITORY" ]]; then ghr_resolve_repository ""; fi
  if [[ "$RELEASE_REMOTE" == false ]]; then ghr_require_remote_commit "$COMMIT_HASH"; fi
  ghr_check_existing "$VERSION"
}

create_github_release() {
  echo
  echo "GitHub release:"
  printf '  Repository:  %s\n' "$GHR_REPOSITORY"
  printf '  Tag:         %s\n' "$VERSION"
  printf '  Commit:      %s\n' "$COMMIT_HASH"
  printf '  Asset:       %s\n' "$(basename "$ARCHIVE")"
  echo

  ghr_generate_notes "$VERSION" "$COMMIT_HASH"

  if [[ "$DRY_RUN" == true ]]; then
    ghr_preview "$VERSION" "$COMMIT_HASH"
    echo
    echo "Dry run: no GitHub changes made."
    GHR_COMPLETED=true
    return
  fi

  ghr_publish "$VERSION" "$COMMIT_HASH" "$ARCHIVE"
}

main() {
  parse_args "$@"
  release_validate_options "$MODE"

  require_commands git jq zip
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  prepare_repository
  read_composer_metadata

  if [[ "$MODE" == "gh-release" ]]; then
    prepare_github
  fi

  create_archive

  if [[ "$MODE" == "gh-release" ]]; then
    create_github_release
  fi
}

main "$@"
