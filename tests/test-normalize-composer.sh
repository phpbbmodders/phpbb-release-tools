#!/usr/bin/bash
# Offline checks for normalize-composer.py on fixture extensions.
# Usage: tests/test-normalize-composer.sh
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tool="$root/normalize-composer.py"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

failures=0
fail() { printf 'FAIL: %s\n' "$1"; failures=$((failures + 1)); }
pass() { printf 'ok: %s\n' "$1"; }

# expect NAME FILE EXPECTED-CONTENT: compare a file byte for byte.
expect() {
	if [[ "$(cat "$2")" == "$3" ]]; then pass "$1"; else fail "$1"; diff <(printf '%s' "$3") "$2" || true; fi
}

# A typical extension: tabs, phpbb/phpbb in require, old constraints, no ext.php ceiling.
mkdir -p "$work/plain"
printf '%s\n' '{' \
	'	"name": "acme/plain",' \
	'	"type": "phpbb-extension",' \
	'	"keywords": ["phpbb", "extension"],' \
	'	"require": {' \
	'		"php": ">=7.4",' \
	'		"phpbb/phpbb": ">=3.3",' \
	'		"composer/installers": "~1.0"' \
	'	},' \
	'	"extra": {' \
	'		"display-name": "Plain",' \
	'		"soft-require": {' \
	'			"phpbb/phpbb": ">=3.3"' \
	'		}' \
	'	}' \
	'}' >"$work/plain/composer.json"
cp "$work/plain/composer.json" "$work/plain.orig"

"$tool" --dry-run "$work/plain" >/dev/null
if cmp -s "$work/plain/composer.json" "$work/plain.orig"; then pass "--dry-run writes nothing"; else fail "--dry-run writes nothing"; fi

"$tool" "$work/plain" >/dev/null
expect "typical extension: only the agreed keys change" "$work/plain/composer.json" "$(printf '%s\n' '{' \
	'	"name": "acme/plain",' \
	'	"type": "phpbb-extension",' \
	'	"keywords": ["phpbb", "extension"],' \
	'	"require": {' \
	'		"php": ">=8.3.36",' \
	'		"composer/installers": "^1.0 || ^2.0"' \
	'	},' \
	'	"extra": {' \
	'		"display-name": "Plain",' \
	'		"soft-require": {' \
	'			"phpbb/phpbb": ">=3.3.19"' \
	'		}' \
	'	}' \
	'}')"

out=$("$tool" "$work/plain")
if [[ "$out" == "plain: no changes" ]]; then pass "a second run changes nothing"; else fail "a second run changes nothing: $out"; fi

# CRLF line endings, mixed indentation, no composer/installers, ext.php ceiling at 4.0.
mkdir -p "$work/crlf"
printf '%s\r\n' '{' \
	'    "name": "acme/crlf",' \
	'    "type": "phpbb-extension",' \
	'    "require": {' \
	'        "php": ">=5.3.3",' \
	'		"ext-dom": "*"' \
	'    },' \
	'    "extra": {' \
	'        "display-name": "CRLF",' \
	'        "soft-require": {' \
	'            "phpbb/phpbb": ">=3.2"' \
	'        }' \
	'    }' \
	'}' >"$work/crlf/composer.json"
printf '%s\n' '<?php' "return phpbb_version_compare(PHPBB_VERSION, '4.0.0-dev', '<');" >"$work/crlf/ext.php"
"$tool" "$work/crlf" >/dev/null
expect "CRLF kept, installers added after php, other lines untouched, ceiling from ext.php" "$work/crlf/composer.json" \
	"$(printf '%s\r\n' '{' \
		'    "name": "acme/crlf",' \
		'    "type": "phpbb-extension",' \
		'    "require": {' \
		'        "php": ">=8.3.36",' \
		'        "composer/installers": "^1.0 || ^2.0",' \
		'		"ext-dom": "*"' \
		'    },' \
		'    "extra": {' \
		'        "display-name": "CRLF",' \
		'        "soft-require": {' \
		'            "phpbb/phpbb": ">=3.3.19,<4.0.0@dev"' \
		'        }' \
		'    }' \
		'}')"

# Single-line objects stay on one line; an existing <4.1 bound is kept.
mkdir -p "$work/inline"
printf '%s\n' '{' \
	'    "name": "acme/inline",' \
	'    "type": "phpbb-extension",' \
	'    "require": {"php": ">=7.2"},' \
	'    "extra": {' \
	'        "display-name": "Inline",' \
	'        "soft-require": {"phpbb/phpbb": ">=3.3.0,<4.1.0@dev"}' \
	'    }' \
	'}' >"$work/inline/composer.json"
"$tool" "$work/inline" >/dev/null
expect "single-line objects stay single-line; 4.0 support kept" "$work/inline/composer.json" "$(printf '%s\n' '{' \
	'    "name": "acme/inline",' \
	'    "type": "phpbb-extension",' \
	'    "require": {"php": ">=8.3.36", "composer/installers": "^1.0 || ^2.0"},' \
	'    "extra": {' \
	'        "display-name": "Inline",' \
	'        "soft-require": {"phpbb/phpbb": ">=3.3.19,<4.1.0@dev"}' \
	'    }' \
	'}')"

# --upper sets the bound explicitly.
cp "$work/plain.orig" "$work/plain/composer.json"
"$tool" --upper '<4.1.0@dev' "$work/plain" >/dev/null
if grep -q '"phpbb/phpbb": ">=3.3.19,<4.1.0@dev"' "$work/plain/composer.json"; then
	pass "--upper sets the soft-require bound"
else
	fail "--upper sets the soft-require bound"
fi

# Not an extension: skipped, exit 1, file untouched.
mkdir -p "$work/library"
printf '%s\n' '{"name": "acme/library", "type": "library", "require": {"php": ">=7.4"}}' >"$work/library/composer.json"
cp "$work/library/composer.json" "$work/library.orig"
status=0
"$tool" "$work/library" >/dev/null || status=$?
if [[ $status -eq 1 ]] && cmp -s "$work/library/composer.json" "$work/library.orig"; then
	pass "a non-extension is skipped with exit 1"
else
	fail "a non-extension is skipped with exit 1 (exit $status)"
fi

# No arguments: help, exit 2. Bad --upper: exit 2.
status=0
"$tool" >/dev/null || status=$?
if [[ $status -eq 2 ]]; then pass "no arguments prints help and exits 2"; else fail "no arguments exits 2 (exit $status)"; fi
status=0
"$tool" --upper 4.1 "$work/plain" >/dev/null 2>&1 || status=$?
if [[ $status -eq 2 ]]; then pass "a malformed --upper exits 2"; else fail "a malformed --upper exits 2 (exit $status)"; fi

if ((failures)); then
	printf '%d test(s) failed\n' "$failures"
	exit 1
fi
echo "All tests passed."
