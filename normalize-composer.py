#!/usr/bin/env python3
"""
Bring phpBB extensions' composer.json to the phpbbmodders baseline.

For each extension checkout given, this changes only:
  - require.php                       set to --php (default ">=8.3.36")
  - require.composer/installers       set to --installers (default "^1.0 || ^2.0"),
                                      added after "php" when missing
  - require.phpbb/phpbb               removed (phpBB belongs in soft-require only)
  - extra.soft-require.phpbb/phpbb    ">=" --phpbb-min (default 3.3.19), plus an
                                      upper bound chosen by --upper

The upper bound with "--upper auto" (the default) follows what the extension
enforces: an ext.php check refusing phpBB 4.0.0-dev gives "<4.0.0@dev", one
refusing 4.1.0-dev gives "<4.1.0@dev". With no ceiling in ext.php, an existing
"<4.1" bound in soft-require (4.0 support already declared) is kept as
"<4.1.0@dev"; otherwise no upper bound is written, so composer.json does not
claim phpBB 4.0 support nobody has checked.

The file is edited as text: key order, indentation, line endings and every
other field stay as they are. The edited file is parsed again and must equal the original with
exactly these changes, or nothing is written.

Inputs:  one or more extension checkout directories (each with composer.json)
Outputs: one line per extension with its changes, or "no changes"; with
         --dry-run nothing is written
Exit status: 0 when every extension was handled, 1 when one was skipped
             (problem printed), 2 for bad arguments.
"""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

PHPBB = "phpbb/phpbb"
INSTALLERS = "composer/installers"


def object_span(text: str, path: tuple) -> tuple:
    """Return (start, end) of the JSON object at a key path, braces included.

    Scans the text with string awareness. Raises ValueError when the path is
    missing or does not lead to an object.
    """
    start, end = 0, len(text)
    for key in path:
        depth, i, found = 0, start, None
        while i < end:
            ch = text[i]
            if ch == '"':
                j = i + 1
                while text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                if depth == 1 and json.loads(text[i:j + 1]) == key and re.match(r"\s*:", text[j + 1:]):
                    found = j + 1
                    break
                i = j + 1
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            i += 1
        if found is None:
            raise ValueError(f"no {'.'.join(path)} object")
        brace = text.index("{", found)
        if text[found:brace].strip(" \t\r\n:"):
            raise ValueError(f"{'.'.join(path)} is not an object")
        depth, i = 0, brace
        while True:
            ch = text[i]
            if ch == '"':
                i += 1
                while text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    start, end = brace, i + 1
                    break
            i += 1
    return start, end


ENTRY = re.compile(r'^(?P<indent>[ \t]+)"(?P<key>(?:[^"\\]|\\.)*)"(?P<sep>\s*:\s*)"(?P<value>(?:[^"\\]|\\.)*)",?[ \t]*$')


def rewrite_flat_object(text: str, path: tuple, change) -> str:
    """Rewrite a one-entry-per-line object of string values.

    change(entries) receives and returns a list of [key, value] pairs. The
    entries keep the object's own indentation and key/value spacing.
    """
    start, end = object_span(text, path)
    body = text[start + 1:end - 1]
    if "\n" not in body:
        # Single-line object such as {"php": ">=7.2"}: keep it on one line.
        pairs = re.findall(r'"((?:[^"\\]|\\.)*)"(\s*:\s*)"((?:[^"\\]|\\.)*)"', body)
        if not pairs or re.sub(r'"(?:[^"\\]|\\.)*"\s*:\s*"(?:[^"\\]|\\.)*"', "", body).strip(" ,"):
            raise ValueError(f"{'.'.join(path)} is not a flat object of strings: {body.strip()!r}")
        sep = pairs[0][1]
        entries = change([[json.loads(f'"{k}"'), json.loads(f'"{v}"')] for k, _, v in pairs])
        return (text[:start + 1] + ", ".join(f"{json.dumps(k)}{sep}{json.dumps(v, ensure_ascii=False)}"
                                             for k, v in entries) + text[end - 1:])
    lines = [line for line in body.split("\n") if line.strip()]
    entries, original = [], {}
    for line in lines:
        m = ENTRY.match(line.rstrip("\r"))
        if not m:
            raise ValueError(f"{'.'.join(path)} is not one string entry per line: {line.strip()!r}")
        key, value = json.loads(f'"{m["key"]}"'), json.loads(f'"{m["value"]}"')
        entries.append([key, value])
        # Each line keeps its own indentation, spacing and escaping.
        original[key] = (m["indent"], m["sep"], f'"{m["key"]}"', f'"{m["value"]}"', value)
    if not entries:
        raise ValueError(f"{'.'.join(path)} is empty")
    closing = body[body.rstrip(" \t").rfind("\n") + 1:]
    out, indent, sep = [], original[entries[0][0]][0], original[entries[0][0]][1]
    for key, value in change(entries):
        if key in original:
            indent, sep, raw_key, raw_value, old_value = original[key]
            raw_value = raw_value if value == old_value else json.dumps(value, ensure_ascii=False)
        else:
            # A new entry takes the indentation of the line before it.
            raw_key, raw_value = json.dumps(key), json.dumps(value, ensure_ascii=False)
        out.append(f"{indent}{raw_key}{sep}{raw_value}")
    new_body = "\n" + ",\n".join(out) + "\n" + closing
    return text[:start + 1] + new_body + text[end - 1:]


def upper_bound(repo: Path, current_soft: str, mode: str) -> str:
    """Return the soft-require upper bound (with leading ',') or '' for none."""
    if mode != "auto":
        return "" if mode == "none" else "," + mode
    ext_php = (repo / "ext.php").read_text() if (repo / "ext.php").is_file() else ""
    ceiling = re.search(r"PHPBB_VERSION,\s*'([^']+)',\s*'<'", ext_php)
    if ceiling:
        return f",<{ceiling.group(1).replace('-dev', '')}@dev"
    if re.search(r"<\s*4\.1", current_soft):
        return ",<4.1.0@dev"
    return ""


def normalize(repo: Path, args: argparse.Namespace) -> list:
    """Edit one extension's composer.json; return the list of changes made."""
    path = repo / "composer.json"
    with open(path, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    # Work on LF text and write back the file's own line endings, so a CRLF
    # file isn't renormalized as a side effect.
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    doc = json.loads(text)
    if doc.get("type") != "phpbb-extension":
        raise ValueError("not a phpbb-extension")
    require = doc.get("require", {})
    current_soft = doc.get("extra", {}).get("soft-require", {}).get(PHPBB)
    if current_soft is None:
        raise ValueError("no extra.soft-require.phpbb/phpbb")
    if "php" not in require:
        raise ValueError("no require.php")
    soft = f">={args.phpbb_min}{upper_bound(repo, current_soft, args.upper)}"

    changes = []
    expected = copy.deepcopy(doc)

    def fix_require(entries: list) -> list:
        out = []
        for key, value in entries:
            if key == PHPBB:
                changes.append(f"removed require {PHPBB} ({value})")
                continue
            if key == "php" and value != args.php:
                changes.append(f"php {value} -> {args.php}")
                value = args.php
            if key == INSTALLERS and value != args.installers:
                changes.append(f"{INSTALLERS} {value} -> {args.installers}")
                value = args.installers
            out.append([key, value])
            if key == "php" and INSTALLERS not in require:
                changes.append(f"added {INSTALLERS} {args.installers}")
                out.append([INSTALLERS, args.installers])
        return out

    def fix_soft(entries: list) -> list:
        for entry in entries:
            if entry[0] == PHPBB and entry[1] != soft:
                changes.append(f"soft-require {entry[1]} -> {soft}")
                entry[1] = soft
        return entries

    new_text = rewrite_flat_object(text, ("require",), fix_require)
    new_text = rewrite_flat_object(new_text, ("extra", "soft-require"), fix_soft)

    # The edited file must parse to the original plus exactly these changes.
    new_require = {}
    for key, value in expected["require"].items():
        if key == PHPBB:
            continue
        new_require[key] = args.php if key == "php" else args.installers if key == INSTALLERS else value
        if key == "php" and INSTALLERS not in require:
            new_require[INSTALLERS] = args.installers
    expected["require"] = new_require
    expected["extra"]["soft-require"][PHPBB] = soft
    result = json.loads(new_text)
    if result != expected or list(result["require"]) != list(expected["require"]):
        raise ValueError("edited composer.json does not match the expected result; nothing written")
    if changes and not args.dry_run:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(new_text.replace("\n", newline))
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bring phpBB extensions' composer.json to the phpbbmodders baseline "
                    "(require.php, composer/installers, no phpbb/phpbb in require, soft-require floor).",
        epilog="Exit status: 0 all handled, 1 an extension was skipped, 2 bad arguments.")
    ap.add_argument("repos", nargs="*", metavar="DIR", help="extension checkout directory containing composer.json")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print the changes without writing any file")
    ap.add_argument("-P", "--php", default=">=8.3.36", help='require.php constraint (default: ">=8.3.36")')
    ap.add_argument("-i", "--installers", default="^1.0 || ^2.0",
                    help='composer/installers constraint (default: "^1.0 || ^2.0")')
    ap.add_argument("-m", "--phpbb-min", default="3.3.19", help="lowest phpBB version for soft-require (default: 3.3.19)")
    ap.add_argument("-u", "--upper", default="auto",
                    help='soft-require upper bound: "auto" (follow ext.php, see above), "none", or a '
                         'constraint such as "<4.1.0@dev" (default: auto)')
    args = ap.parse_args()
    if not args.repos:
        ap.print_help()
        return 2
    if args.upper not in ("auto", "none") and not args.upper.startswith("<"):
        ap.error('--upper must be "auto", "none" or start with "<"')

    skipped = 0
    for repo in (Path(r).resolve() for r in args.repos):
        try:
            changes = normalize(repo, args)
        except (OSError, ValueError, KeyError) as e:
            print(f"{repo.name}: SKIPPED: {e}")
            skipped += 1
            continue
        print(f"{repo.name}: " + ("; ".join(changes) if changes else "no changes")
              + (" (dry run)" if args.dry_run and changes else ""))
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
