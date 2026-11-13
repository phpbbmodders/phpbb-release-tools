#!/usr/bin/bash
#
# Run extensions' feature checks on every phpBB branch and database seed-forum
# supports: for each branch and database, build a seed-forum board with the
# extensions enabled (bind-mounted from their checkouts, as they are on disk),
# run feature_checks.py --seed-board against it (checks plus a
# disable/purge/enable round trip), then unmount its extensions and stop its
# database container.
# Prints one result line per board and a summary.
#
# Needs a seed-forum checkout with board.database support, Docker, and what
# seed-forum and feature_checks.py need. MSSQL accepts Microsoft's SQL Server
# Developer licence (ACCEPT_EULA=Y) in its container; leave mssql out of -d if
# that hasn't been agreed. Oracle isn't covered: phpBB is dropping it.
#
# Without arguments it shows help.
# Exit status: 0 when every board passed, 1 when one failed, 2 for bad arguments.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCHES="3.3,master"
DATABASES="sqlite,mysql,mariadb,postgres,mssql"
WORK_DIR="$HOME/.cache/phpbb-release-tools/db-matrix"
SEED_FORUM="$(cd "$SCRIPT_DIR/../.." && pwd)/seed-forum"
BASE_PORT=8130
KEEP=0
PHPBB_PULL_REQUEST=""

usage() {
	cat <<'EOF'
Usage: db-matrix.sh [options] EXTENSION_CHECKOUT...

Run each extension's feature checks on every phpBB branch and database.

Options:
  -b, --branches LIST     phpBB branches, comma-separated (default: 3.3,master)
  -d, --databases LIST    databases, comma-separated (default: sqlite,mysql,mariadb,postgres,mssql)
  -w, --work-dir DIR      where boards and their configs go (default: ~/.cache/phpbb-release-tools/db-matrix)
  -s, --seed-forum DIR    seed-forum checkout (default: seed-forum next to this repository)
  -p, --base-port PORT    first port for the boards; each board gets the next one (default: 8130)
  -P, --phpbb-pull-request N
                          build the master boards from open phpbb/phpbb pull request N
                          (seed-forum board.phpbb_pull_request); their names get -prN
  -k, --keep              leave the extensions mounted and the database containers running after the checks
  -h, --help              show this help and exit

Boards are kept in the work dir between runs, so phpBB is only downloaded once
per branch and database. Each run rebuilds them from scratch.
EOF
}

die() {
	echo "Error: $*" >&2
	exit 2
}

EXTENSIONS=()
while [ $# -gt 0 ]; do
	case "$1" in
		-b|--branches) BRANCHES="${2:?}"; shift 2 ;;
		-d|--databases) DATABASES="${2:?}"; shift 2 ;;
		-w|--work-dir) WORK_DIR="${2:?}"; shift 2 ;;
		-s|--seed-forum) SEED_FORUM="${2:?}"; shift 2 ;;
		-p|--base-port) BASE_PORT="${2:?}"; shift 2 ;;
		-P|--phpbb-pull-request) PHPBB_PULL_REQUEST="${2:?}"; shift 2 ;;
		-k|--keep) KEEP=1; shift ;;
		-h|--help) usage; exit 0 ;;
		-*) die "unknown option $1 (see --help)" ;;
		*) EXTENSIONS+=("$(cd "$1" && pwd)"); shift ;;
	esac
done
if [ ${#EXTENSIONS[@]} -eq 0 ]; then
	usage
	exit 0
fi
[[ "$BASE_PORT" =~ ^[0-9]+$ ]] || die "--base-port must be a number"
[ -z "$PHPBB_PULL_REQUEST" ] || [[ "$PHPBB_PULL_REQUEST" =~ ^[1-9][0-9]*$ ]] || die "--phpbb-pull-request must be a pull request number"
[ -f "$SEED_FORUM/bin/db-container.py" ] || die "$SEED_FORUM has no board.database support (bin/db-container.py)"
for ext in "${EXTENSIONS[@]}"; do
	[ -f "$ext/composer.json" ] || die "$ext has no composer.json"
done

mkdir -p "$WORK_DIR"
IFS=, read -r -a branch_list <<< "$BRANCHES"
IFS=, read -r -a database_list <<< "$DATABASES"

# One YAML config per board, listing the extensions by a label from their names.
write_config() {
	local file="$1" root="$2" branch="$3" database="$4" port="$5"
	{
		echo "# Written by phpbb-release-tools db-matrix.sh; rewritten on every run."
		echo "board:"
		echo "  root: '$root'"
		echo "  phpbb_branch: '$branch'"
		if [ "$branch" = master ] && [ -n "$PHPBB_PULL_REQUEST" ]; then
			echo "  phpbb_pull_request: $PHPBB_PULL_REQUEST"
		fi
		echo "  database: $database"
		echo "  server_name: localhost"
		echo "  server_port: '$port'"
		echo "  seeds:"
		echo "    - bin/seed-standard-fixtures.php"
		echo "styles: {}"
		echo "extensions:"
		for ext in "${EXTENSIONS[@]}"; do
			echo "  $(jq -r '.name' "$ext/composer.json" | tr '/' '_'):"
			echo "    source: '$ext'"
		done
	} > "$file"
}

summary=()
failed=0
port="$BASE_PORT"
for branch in "${branch_list[@]}"; do
	for database in "${database_list[@]}"; do
		name="${branch//./}-$database"
		# A pull-request board keeps its own folder, as seed-forum refuses to
		# reuse a folder filled from another phpBB source.
		if [ "$branch" = master ] && [ -n "$PHPBB_PULL_REQUEST" ]; then
			name="${branch//./}-pr$PHPBB_PULL_REQUEST-$database"
		fi
		root="$WORK_DIR/boards/$name"
		config="$WORK_DIR/$name.yml"
		log="$WORK_DIR/$name.log"
		if ss -ltn | grep -q ":$port "; then
			echo "== $name: port $port is in use; skipping" >&2
			summary+=("$name: SKIPPED (port $port in use)")
			failed=1
			port=$((port + 1))
			continue
		fi
		mkdir -p "$root"
		write_config "$config" "$root" "$branch" "$database" "$port"
		echo "== $name: building the board on port $port (log: $log)"
		if ! python3 "$SEED_FORUM/bin/seed-forum.py" --config "$config" --reset > "$log" 2>&1; then
			echo "   board build FAILED; last lines of the log:"
			tail -15 "$log" | sed 's/^/   /'
			summary+=("$name: BUILD FAILED (see $log)")
			failed=1
		else
			echo "== $name: running the checks"
			if "$SCRIPT_DIR/feature_checks.py" "${EXTENSIONS[@]}" --seed-board "$root" --port "$port" \
					--seed-forum "$SEED_FORUM" 2>&1 | tee -a "$log"; then
				summary+=("$name: passed")
			else
				summary+=("$name: FAILED (see $log)")
				failed=1
			fi
		fi
		if [ "$KEEP" = 0 ]; then
			# Unmount the extensions, so deleting the work dir can never reach a checkout
			python3 "$SEED_FORUM/bin/seed-forum.py" --config "$config" --unmount >> "$log" 2>&1 \
				|| echo "   Warning: could not unmount the extensions from $root; see $log" >&2
			if [ "$database" != sqlite ]; then
				python3 "$SEED_FORUM/bin/db-container.py" stop "$root" >> "$log" 2>&1 || true
			fi
		fi
		port=$((port + 1))
	done
done

echo "== Summary"
printf '   %s\n' "${summary[@]}"
exit "$failed"
