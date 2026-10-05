#!/usr/bin/bash
# git-extensions.sh
#
# Build a phpBB extension release archive from the current Git commit and,
# optionally, publish it as a GitHub Release.
#
# Usage:
#   git-extensions.sh --create
#   git-extensions.sh --gh-release
#   git-extensions.sh --gh-release --dry-run
#   git-extensions.sh --help
#
# The archive is created from HEAD, so uncommitted changes are not included.
# Files marked export-ignore in .gitattributes are excluded. It can be run from
# anywhere inside the repository.
#
# The "name" (vendor/name) and "version" in the committed composer.json name
# the ZIP, and the version is the tag and release name. Alpha, beta, RC, and dev
# versions are published as prereleases. A version ending in -dev is a rolling
# prerelease: each --gh-release moves its tag to HEAD, regenerates its notes,
# and replaces its ZIP in the same release. Other versions are immutable, so an
# existing tag or release is an error. HEAD must already be pushed to GitHub.
#
# Output:
#   ../git-exported/<vendor>-<name>-<version>.zip, with a <vendor>/<name>/ folder
#
# Requirements:
#   --create:      git, jq, zip
#   --gh-release:  also gh (authenticated) and timeout
#   lib/github-release.sh next to this script (symlinking the script works).
#
# Exit status: 0 on success, 1 on failure, 2 on invalid usage. No ZIP is left
# behind on failure.

set -Eeuo pipefail

readonly GIT_EXPORT_DIR="../git-exported"

# Shared release logic lives next to the real script, even when it is run through a symlink.
SCRIPT_DIR="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
readonly SCRIPT_DIR
# shellcheck source=lib/github-release.sh
source "$SCRIPT_DIR/lib/github-release.sh"

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
  cat <<'EOF'
Usage:
  git-extensions.sh --create
  git-extensions.sh --gh-release
  git-extensions.sh --gh-release --dry-run
  git-extensions.sh --help

Options:
  -C, --create       Create the release ZIP locally.
  -g, --gh-release   Create the ZIP and publish it as a GitHub Release.
  -n, --dry-run      With --gh-release, validate and show what would be
                     published without making changes on GitHub.
  -h, --help         Show this help.

The version in composer.json is the tag and release name. Alpha, beta, RC,
and dev versions are prereleases. A -dev version is a rolling prerelease: each
--gh-release moves its tag to HEAD and replaces its notes and ZIP in the same
release. Any other existing tag or release is an error.

To publish the remote default branch instead of local HEAD, use
git-release-api.sh.

Examples:
  git-extensions.sh --create
  git-extensions.sh --gh-release
  git-extensions.sh --gh-release --dry-run
EOF
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
        [[ -z "$MODE" ]] || usage_error "only one of --create or --gh-release may be used"
        MODE="create"
        ;;
      -g|--gh-release)
        [[ -z "$MODE" ]] || usage_error "only one of --create or --gh-release may be used"
        MODE="gh-release"
        ;;
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

  [[ -n "$MODE" ]] || usage_error "one of --create or --gh-release is required"
  if [[ "$DRY_RUN" == true && "$MODE" != "gh-release" ]]; then
    usage_error "--dry-run requires --gh-release"
  fi
}

# Work from the repository root, so the script also works from a subdirectory.
prepare_repository() {
  local repo_root
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || die "the current directory is not inside a git repository"
  cd "$repo_root"

  # Archive the commit itself; this also works on a detached HEAD and on branch names containing "/".
  git rev-parse --verify -q HEAD >/dev/null || die "the repository has no commits"
  COMMIT_HASH="$(git rev-parse HEAD)"

  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    if [[ "$MODE" == "gh-release" ]]; then
      die "tracked files contain uncommitted changes"
    fi
    echo "Warning: uncommitted tracked changes are not included; only HEAD is packaged." >&2
  fi
}

# Read vendor, name, and version from the committed composer.json (the same commit that is archived).
read_composer_metadata() {
  local composer_json package
  composer_json="$(git show HEAD:composer.json 2>/dev/null)" \
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

  mkdir -p "$GIT_EXPORT_DIR" || die "failed to create $GIT_EXPORT_DIR"
  output_dir="$(cd "$GIT_EXPORT_DIR" && pwd)" || die "failed to resolve $GIT_EXPORT_DIR"

  ARCHIVE="$output_dir/$filename"
  PARTIAL="$output_dir/.$filename.partial"

  git archive --format=zip -9 --prefix="$VENDOR/$RELEASE_NAME/" --output="$PARTIAL" HEAD \
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

  GHR_SCRATCH="$(mktemp -d)"
  ghr_resolve_repository ""
  ghr_require_remote_commit "$COMMIT_HASH"
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

  require_commands git jq zip
  trap cleanup EXIT

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
