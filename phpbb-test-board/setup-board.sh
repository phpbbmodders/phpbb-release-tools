#!/usr/bin/bash
# setup-board.sh
#
# Builds a local phpBB test board for smoke_test.py, feature_checks.py and
# screenshots.py.
#
# Clones a phpBB release (default: the current stable 3.3 release from
# https://version.phpbb.com/phpbb/versions.json), installs its Composer
# dependencies, and installs the board onto SQLite with no extensions
# enabled. The admin password is generated and written, with the board's
# paths, to DIR/board.env (mode 600); the scripts read that file, so the
# password is never printed. A copy of the fresh database is kept as
# DIR/board.clean.sqlite3 so every test starts from the same state.
#
# The board is for local testing only: never expose it to a network.
#
# Usage:   setup-board.sh -d DIR [-v VERSION] [-f]
# Outputs: DIR/phpbb/ (the board), DIR/board.sqlite3, DIR/board.clean.sqlite3,
#          DIR/board.env
# Exit status: 0 on success, 1 on failure, 2 for bad arguments.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: setup-board.sh -d DIR [-v VERSION] [-f]

Build a local phpBB test board for smoke_test.py, feature_checks.py and
screenshots.py.

Options:
  -d, --dir DIR          directory to build the board in (required)
  -v, --version VERSION  phpBB release to install, for example 3.3.19
                         (default: the current stable 3.3 release)
  -f, --force            replace an existing board in DIR
  -h, --help             show this help and exit

Afterwards, point the scripts at the board with --board-dir DIR or
PHPBB_TEST_BOARD=DIR.
EOF
}

dir=""
version=""
force=0
while [ $# -gt 0 ]; do
  case "$1" in
    -d | --dir) dir="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    -v | --version) version="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    -f | --force) force=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$dir" ]; then
  echo "error: --dir is required" >&2
  usage >&2
  exit 2
fi
if [ -n "$version" ] && ! [[ "$version" =~ ^3\.3\.[0-9]+$ ]]; then
  echo "error: --version must look like 3.3.19" >&2
  exit 2
fi

for tool in git php composer curl jq openssl; do
  command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 1; }
done

mkdir -p "$dir"
dir="$(cd "$dir" && pwd)"
if [ -e "$dir/phpbb" ]; then
  if [ "$force" -ne 1 ]; then
    echo "error: $dir/phpbb already exists (use --force to replace it)" >&2
    exit 1
  fi
  rm -rf -- "$dir/phpbb" "$dir/board.sqlite3" "$dir/board.clean.sqlite3" "$dir/board.env" "$dir/install.yml"
fi

if [ -z "$version" ]; then
  version="$(curl -fsS --max-time 30 https://version.phpbb.com/phpbb/versions.json | jq -r '.stable["3.3"].current')"
  [[ "$version" =~ ^3\.3\.[0-9]+$ ]] || { echo "error: could not read the current phpBB 3.3 release" >&2; exit 1; }
fi
echo "Installing phpBB $version into $dir"

git clone -q --depth 1 --branch "release-$version" https://github.com/phpbb/phpbb.git "$dir/phpbb"
(cd "$dir/phpbb/phpBB" && composer install -q --no-dev --no-interaction --ignore-platform-req=php)

password="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)"
umask 077
cat >"$dir/install.yml" <<EOF
installer:
  admin:
    name: admin
    password: $password
    email: admin@example.com
  board:
    lang: en
    name: phpBB test board
    description: Local test board
  database:
    dbms: sqlite3
    dbhost: $dir/board.sqlite3
    dbport: ~
    dbuser: ~
    dbpasswd: ~
    dbname: ~
    table_prefix: phpbb_
  email:
    enabled: false
  server:
    cookie_secure: false
    server_protocol: http://
    force_server_vars: false
    server_name: localhost
    server_port: 8083
    script_path: /
  extensions: []
EOF

(cd "$dir/phpbb/phpBB" && php install/phpbbcli.php install "$dir/install.yml" >/dev/null)
rm -f -- "$dir/install.yml"
mv "$dir/phpbb/phpBB/install" "$dir/phpbb/phpBB/install_off"
cp "$dir/board.sqlite3" "$dir/board.clean.sqlite3"

cat >"$dir/board.env" <<EOF
PHPBB_BOARD=$dir/phpbb/phpBB
PHPBB_DB=$dir/board.sqlite3
PHPBB_CLEAN_DB=$dir/board.clean.sqlite3
PHPBB_ADMIN_USER=admin
PHPBB_ADMIN_PASSWORD=$password
EOF

echo "Done. Use: --board-dir $dir  (or PHPBB_TEST_BOARD=$dir)"
