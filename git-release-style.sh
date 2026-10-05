#!/usr/bin/bash
# git-release-style.sh
#
# Build a phpBB style release archive from the current Git commit and,
# optionally, publish it as a GitHub Release.
#
# Usage:
#   git-release-style.sh --create
#   git-release-style.sh --gh-release
#   git-release-style.sh --gh-release --dry-run
#   git-release-style.sh --help
#
# Modes:
#   --create
#       Create the release ZIP locally.
#
#   --gh-release
#       Create the release ZIP and publish it as a GitHub Release.
#
#   --gh-release --dry-run
#       Create the release ZIP and validate the GitHub release operation,
#       but make no changes on GitHub.
#
# The style_version from style.cfg is the tag and release name. Alpha, beta,
# RC, and dev versions are published as prereleases. A version ending in -dev
# is a rolling prerelease: each --gh-release moves its tag to HEAD, regenerates
# its notes, and replaces its ZIP in the same release. Other versions are
# immutable, so an existing tag or release is an error. HEAD must already be
# pushed to GitHub.
#
# The archive is created from HEAD, so uncommitted changes are not included.
# Files marked export-ignore in .gitattributes are excluded.
#
# Output:
#   ../git-exported/<Stylename>_<version>.zip
#
# Requirements:
#   --create:      git, zip
#   --gh-release:  git, zip, gh (authenticated), jq, timeout
#   lib/github-release.sh next to this script (symlinking the script works).
#
# Exit status:
#   0 on success
#   1 on failure
#   2 on invalid usage

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
STYLE_NAME=""
STYLE_VERSION=""
COMMIT_HASH=""
PARTIAL=""
GHR_SCRATCH=""

die() {
	echo "Error: $*" >&2
	exit 1
}

# Remove partial output and GitHub scratch files, and name any release a failed
# run left behind.
cleanup() {
	[[ -z "$PARTIAL" ]] || rm -f -- "$PARTIAL"
	[[ -z "$GHR_SCRATCH" ]] || rm -rf -- "$GHR_SCRATCH"
	ghr_report_incomplete
}

usage_error() {
	echo "Error: $*" >&2
	echo "Run with --help for usage." >&2
	exit 2
}

usage() {
	cat <<'EOF'
Usage:
  git-release-style.sh --create
  git-release-style.sh --gh-release
  git-release-style.sh --gh-release --dry-run
  git-release-style.sh --help

Options:
  -C, --create       Create the release ZIP locally.
  -g, --gh-release   Create the ZIP and publish it as a GitHub Release.
  -n, --dry-run      With --gh-release, validate and show what would be
                     published without making changes on GitHub.
  -h, --help         Show this help.

The style_version in style.cfg is the tag and release name. Alpha, beta, RC,
and dev versions are prereleases. A -dev version is a rolling prerelease: each
--gh-release moves its tag to HEAD and replaces its notes and ZIP in the same
release. Any other existing tag or release is an error.

Examples:
  git-release-style.sh --create
  git-release-style.sh --gh-release
  git-release-style.sh --gh-release --dry-run
EOF
}

require_commands() {
	local cmd

	for cmd in "$@"; do
		command -v "$cmd" >/dev/null 2>&1 \
			|| die "required command not found: $cmd"
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
				[[ -z "$MODE" ]] \
					|| usage_error "only one of --create or --gh-release may be used"
				MODE="create"
				;;

			-g|--gh-release)
				[[ -z "$MODE" ]] \
					|| usage_error "only one of --create or --gh-release may be used"
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

	[[ -n "$MODE" ]] \
		|| usage_error "one of --create or --gh-release is required"

	if [[ "$DRY_RUN" == true && "$MODE" != "gh-release" ]]; then
		usage_error "--dry-run requires --gh-release"
	fi
}

read_style_cfg_value() {
	local config="$1"
	local key="$2"
	local value

	value="$(
		awk -F '=' -v key="$key" '
			/^[[:space:]]*[#;]/ {
				next
			}

			{
				current_key = $1
				gsub(/^[[:space:]]+|[[:space:]]+$/, "", current_key)

				if (current_key == key) {
					value = substr($0, index($0, "=") + 1)
					sub(/\r$/, "", value)
					gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
					print value
					exit
				}
			}
		' <<<"$config"
	)"

	[[ -n "$value" ]] || return 1

	printf '%s\n' "$value"
}

prepare_repository() {
	local repo_root

	repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
		|| die "the current directory is not inside a git repository"

	cd "$repo_root"

	git rev-parse --verify -q HEAD >/dev/null \
		|| die "the repository has no commits"

	COMMIT_HASH="$(git rev-parse HEAD)"

	if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
		if [[ "$MODE" == "gh-release" ]]; then
			die "tracked files contain uncommitted changes"
		fi

		echo \
			"Warning: uncommitted tracked changes are not included; only HEAD is packaged." \
			>&2
	fi
}

read_style_metadata() {
	local style_cfg

	style_cfg="$(git show HEAD:style.cfg 2>/dev/null)" \
		|| die "style.cfg is not committed at the repository root"

	STYLE_NAME="$(read_style_cfg_value "$style_cfg" "name")" \
		|| die "style.cfg does not contain a name"

	STYLE_VERSION="$(read_style_cfg_value "$style_cfg" "style_version")" \
		|| die "style.cfg does not contain a style_version"

	if [[ "$STYLE_NAME" == */* ||
	      "$STYLE_NAME" == "." ||
	      "$STYLE_NAME" == ".." ||
	      "$STYLE_NAME" == *$'\n'* ]]; then
		die "invalid style name in style.cfg: $STYLE_NAME"
	fi

	if [[ ! "$STYLE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.-]*$ ]]; then
		die "invalid style_version: $STYLE_VERSION"
	fi
}

create_archive() {
	local filename
	local output_dir

	filename="${STYLE_NAME}_${STYLE_VERSION}.zip"

	mkdir -p "$GIT_EXPORT_DIR" \
		|| die "failed to create $GIT_EXPORT_DIR"

	output_dir="$(cd "$GIT_EXPORT_DIR" && pwd)" \
		|| die "failed to resolve $GIT_EXPORT_DIR"

	ARCHIVE="$output_dir/$filename"
	PARTIAL="$output_dir/.$filename.partial"

	if ! git archive \
		--format=zip \
		-9 \
		--prefix="$STYLE_NAME/" \
		--output="$PARTIAL" \
		HEAD; then
		die "git archive failed"
	fi

	# git archive stores the commit hash as the ZIP archive comment.
	zip -q -z "$PARTIAL" </dev/null \
		|| die "failed to clear the ZIP comment"

	mv -- "$PARTIAL" "$ARCHIVE" \
		|| die "failed to write $ARCHIVE"
	PARTIAL=""

	echo "Release archive created:"
	echo "  $ARCHIVE"
}

# Check GitHub before building: the repository, the pushed commit, and any
# existing release or tag for this version.
prepare_github() {
	require_commands gh jq timeout

	gh auth status >/dev/null 2>&1 \
		|| die "GitHub CLI is not authenticated; run: gh auth login"

	GHR_SCRATCH="$(mktemp -d)"

	ghr_resolve_repository ""
	ghr_require_remote_commit "$COMMIT_HASH"
	ghr_check_existing "$STYLE_VERSION"
}

show_release_plan() {
	echo
	echo "GitHub release:"
	printf '  Repository:  %s\n' "$GHR_REPOSITORY"
	printf '  Tag:         %s\n' "$STYLE_VERSION"
	printf '  Title:       %s\n' "$STYLE_VERSION"
	printf '  Commit:      %s\n' "$COMMIT_HASH"
	printf '  Asset:       %s\n' "$(basename "$ARCHIVE")"
	echo "  Notes:       generated"
}

create_github_release() {
	show_release_plan
	echo

	ghr_generate_notes "$STYLE_VERSION" "$COMMIT_HASH"

	if [[ "$DRY_RUN" == true ]]; then
		ghr_preview "$STYLE_VERSION" "$COMMIT_HASH"
		echo
		echo "Dry run: no GitHub changes made."
		GHR_COMPLETED=true
		return
	fi

	ghr_publish "$STYLE_VERSION" "$COMMIT_HASH" "$ARCHIVE"
}

main() {
	parse_args "$@"

	require_commands git zip
	trap cleanup EXIT

	prepare_repository
	read_style_metadata

	if [[ "$MODE" == "gh-release" ]]; then
		prepare_github
	fi

	create_archive

	if [[ "$MODE" == "gh-release" ]]; then
		create_github_release
	fi
}

main "$@"
