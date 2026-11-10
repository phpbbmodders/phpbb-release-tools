#!/usr/bin/bash
# enable-on-php.sh
#
# Enables phpBB extensions on an existing SQLite test board under a chosen PHP
# version, without that PHP being installed on this machine. phpBB's CLI
# (bin/phpbbcli.php extension:enable) runs in a throwaway Docker container
# from the official php:VERSION-cli image. That command calls the extension's
# is_enableable() and then runs its migrations, so it proves the extension
# installs on that PHP, or shows the refusal message when it does not.
#
# Build the board with seed-forum first (database: sqlite, no extensions).
# The board folder is mounted read-write at its own path, because config.php
# names the SQLite database by its absolute path; each extension checkout is mounted read-only at
# ext/VENDOR/NAME, taken from its composer.json name, so nothing is copied
# into the board. The container has no network, runs as the calling user, and
# is removed when it exits.
#
# Extensions are enabled in the order given; any that fail are retried once
# after the others, so one that depends on another still enables when the
# dependency comes later on the command line. With --disable, every
# extension that enabled is disabled and purged again afterwards, leaving the
# board as it was.
#
# Usage:   enable-on-php.sh [-p VERSION] [-d] BOARD_ROOT EXT_DIR...
# Outputs: the PHP version that ran, then one line per extension (ENABLED or
#          FAILED, with phpBB's message for a failure) on stdout
# Exit status: 0 when every extension enabled, 1 when one failed or Docker
#              failed, 2 for bad arguments.

set -Eeuo pipefail

usage() {
	cat <<'EOF'
Usage: enable-on-php.sh [options] BOARD_ROOT EXT_DIR...

Enable each extension in EXT_DIR... on the SQLite phpBB board at BOARD_ROOT,
running phpBB's CLI under PHP VERSION in a throwaway Docker container
(official php:VERSION-cli image, no network). Extensions are mounted
read-only at ext/VENDOR/NAME from their composer.json name.

Arguments:
  BOARD_ROOT             a seed-forum board using database: sqlite
  EXT_DIR...             extension checkouts (each with a composer.json)

Options:
  -p, --php VERSION      PHP version, as a php:VERSION-cli image tag (default: 8.1)
  -d, --disable          disable and purge every enabled extension afterwards
  -h, --help             show this help and exit

Examples:
  enable-on-php.sh -p 8.1 ~/seeded-board/php81 ~/repos/contactadmin
  enable-on-php.sh -p 8.0 -d ~/seeded-board/php81 ~/repos/zodiacs
EOF
}

usage_error() {
	echo "Error: $*" >&2
	echo "Run with --help for usage." >&2
	exit 2
}

php_version="8.1"
disable_after=0
positional=()

while [ "$#" -gt 0 ]; do
	case "$1" in
		-p|--php)
			[ "$#" -ge 2 ] || usage_error "$1 needs a version"
			php_version="$2"
			shift 2
			;;
		-d|--disable)
			disable_after=1
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		-*)
			usage_error "unknown option: $1"
			;;
		*)
			positional+=("$1")
			shift
			;;
	esac
done

[ "${#positional[@]}" -ge 2 ] || { usage; exit 2; }
[[ "$php_version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || usage_error "PHP version must look like 8.1 or 8.1.34"

board_root=$(realpath -- "${positional[0]}")
if [ ! -f "$board_root/config.php" ] || [ ! -f "$board_root/bin/phpbbcli.php" ]; then
	usage_error "$board_root is not an installed phpBB board"
fi
grep -q "sqlite3" "$board_root/config.php" \
	|| usage_error "$board_root does not use SQLite; only SQLite boards work without a database container"

command -v docker >/dev/null || { echo "Error: docker is not installed" >&2; exit 1; }
command -v jq >/dev/null || { echo "Error: jq is not installed" >&2; exit 1; }

# One read-only mount and one package name per extension.
mounts=()
names=()
for dir in "${positional[@]:1}"; do
	dir=$(realpath -- "$dir")
	[ -f "$dir/composer.json" ] || usage_error "$dir has no composer.json"
	name=$(jq -r '.name // empty' "$dir/composer.json")
	[[ "$name" =~ ^[a-z0-9_-]+/[a-z0-9_-]+$ ]] || usage_error "$dir/composer.json has no valid vendor/name"
	if [ -e "$board_root/ext/$name" ] && [ -n "$(ls -A "$board_root/ext/$name" 2>/dev/null)" ]; then
		usage_error "$board_root/ext/$name already holds files; use a board without that extension"
	fi
	mounts+=(-v "$dir:$board_root/ext/$name:ro")
	names+=("$name")
done

# Runs inside the container: enable each name, retry failures once, report.
read -r -d '' inner <<'EOF' || true
cd "$BOARD_ROOT"
printf 'PHP %s\n' "$(php -r 'echo PHP_VERSION;')"
php bin/phpbbcli.php cache:purge >/dev/null 2>&1 || true
pending=("$@")
for pass in 1 2; do
	failed=()
	for name in "${pending[@]}"; do
		if out=$(php bin/phpbbcli.php --no-ansi extension:enable "$name" 2>&1); then
			echo "ENABLED  $name"
		else
			failed+=("$name")
			[ "$pass" = 2 ] && echo "FAILED   $name: $(echo "$out" | tr -s ' \n' ' ' | sed 's/^ *//')"
		fi
	done
	pending=("${failed[@]}")
	[ "${#pending[@]}" -eq 0 ] && break
done
if [ "$DISABLE_AFTER" = 1 ]; then
	for name in "$@"; do
		php bin/phpbbcli.php extension:disable "$name" >/dev/null 2>&1 || true
		php bin/phpbbcli.php extension:purge "$name" >/dev/null 2>&1 || true
	done
	php bin/phpbbcli.php cache:purge >/dev/null 2>&1 || true
fi
[ "${#pending[@]}" -eq 0 ]
EOF

# Create each mount point as the calling user before Docker starts. Docker
# creates missing mount points as root, and root-owned folders in ext/ could
# then not be removed without root. Only folders made here are removed
# afterwards, deepest first; existing vendor folders are left alone.
created=()
cleanup() {
	local i
	for (( i=${#created[@]}-1; i>=0; i-- )); do
		rmdir "${created[i]}" 2>/dev/null || true
	done
}
trap cleanup EXIT
for name in "${names[@]}"; do
	for path in "$board_root/ext/${name%%/*}" "$board_root/ext/$name"; do
		if [ ! -e "$path" ]; then
			mkdir "$path" || { echo "Error: cannot create $path" >&2; exit 1; }
			created+=("$path")
		fi
	done
done

# The compiled container in cache/production is PHP for the PHP version that
# built it (for example the host's 8.4); an older PHP can't even parse it, so
# start from an empty cache. phpBB rebuilds it on the first request.
find "$board_root/cache/production" -mindepth 1 -delete 2>/dev/null || true

docker run --rm --network none --user "$(id -u):$(id -g)" \
	-e DISABLE_AFTER="$disable_after" \
	-e BOARD_ROOT="$board_root" -v "$board_root:$board_root" "${mounts[@]}" \
	"php:$php_version-cli" bash -c "$inner" enable-on-php "${names[@]}"
