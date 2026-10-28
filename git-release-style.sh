#!/usr/bin/bash
# Build a phpBB style ZIP from local HEAD or the remote default branch.
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
	release_usage 'git-release-style.sh'
	cat <<'HELP'

The name and style_version in root style.cfg name the ZIP and tag.
The ZIP contains the style name folder. export-ignore rules are honored.
HELP
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
					|| usage_error "only one of --create or --release may be used"
				MODE="create"
				;;

			-r|--release|-g|--gh-release)
				[[ -z "$MODE" ]] \
					|| usage_error "only one of --create or --release may be used"
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

	[[ -n "$MODE" ]] \
		|| usage_error "one of --create or --release is required"

	if [[ "$DRY_RUN" == true && "$MODE" != "gh-release" ]]; then
		usage_error "--dry-run requires --release"
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
	release_prepare_source "$MODE"
	COMMIT_HASH="$RELEASE_COMMIT"
}

read_style_metadata() {
	local style_cfg

	style_cfg="$(git show "$COMMIT_HASH":style.cfg 2>/dev/null)" \
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

	mkdir -p "$RELEASE_OUTPUT" \
		|| die "failed to create $RELEASE_OUTPUT"

	output_dir="$(cd "$RELEASE_OUTPUT" && pwd)" \
		|| die "failed to resolve $RELEASE_OUTPUT"

	ARCHIVE="$output_dir/$filename"
	PARTIAL="$output_dir/.$filename.partial"

	if ! git archive \
		--format=zip \
		-9 \
		--prefix="$STYLE_NAME/" \
		--output="$PARTIAL" \
		"$COMMIT_HASH"; then
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

	if [[ -z "$GHR_SCRATCH" ]]; then GHR_SCRATCH="$(mktemp -d)"; fi

	if [[ -z "$GHR_REPOSITORY" ]]; then ghr_resolve_repository ""; fi
	if [[ "$RELEASE_REMOTE" == false ]]; then ghr_require_remote_commit "$COMMIT_HASH"; fi
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
	release_validate_options "$MODE"

	require_commands git zip
	trap cleanup EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM

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
