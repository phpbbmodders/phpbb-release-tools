#!/usr/bin/env python3
"""
Check that a renamed extension upgrades cleanly from its old vendor name.

When an extension moves to a new composer name and PHP namespace (for
example rmcgirr83/stopforumspam to phpbbmodders/stopforumspam), its ext.php
has to move the old install's records over, or phpBB treats it as a new
extension and runs every migration again. This script tests that on a clean
copy of the board (created by setup-board.sh), the way an admin upgrades:

1. Install and enable the old version.
2. Copy the new version beside it; it must refuse to enable while the old
   one is still enabled.
3. Disable the old version, enable the new one, delete the old files.
4. Check the database: migration history renamed and not re-run, other
   migrations' dependencies renamed, ACP modules moved, no duplicates, the
   old extension's record gone, the old version's settings kept, and nothing
   left under the old name (notification types, settings named after it).
5. Serve the board and load the board index, a topic and the extension's
   ACP pages, with no PHP errors.

The board is restored afterwards. Exit status: 0 if every check passed, 1 if
any failed, 2 for bad arguments.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

import requests  # noqa: E402

from _board import Board, ext_name, parse_spec  # noqa: E402

results: list = []


def ok(cond: object, label: str) -> None:
    """Record and print one check result."""
    results.append((bool(cond), label))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


def namespace(name: str) -> str:
    """PHP namespace prefix of an extension: vendor/name -> vendor\\name\\ (no leading backslash)."""
    return name.replace("/", "\\") + "\\"


def renamed(value: str, old_ns: str, new_ns: str) -> str:
    """Map a class name stored with or without a leading backslash to the new namespace."""
    stripped = value.lstrip("\\")
    if stripped.startswith(old_ns):
        return value[:len(value) - len(stripped)] + new_ns + stripped[len(old_ns):]
    return value


def enable(board: Board, name: str) -> str:
    """Enable an extension with phpBB's CLI; return its output."""
    out = board.cli("extension:enable", name)
    board.purge_cache()
    return out


def check_database(board: Board, old: str, new: str, before: dict) -> None:
    """Compare the database after the upgrade with the snapshot taken before it."""
    old_ns, new_ns = namespace(old), namespace(new)
    migrations = {name: (depends, start, end) for name, depends, start, end in board.sql(
        "SELECT migration_name, migration_depends_on, migration_start_time, migration_end_time FROM phpbb_migrations")}

    left = [name for name, (depends, _, _) in migrations.items() if old_ns in name or old_ns in depends.replace("\\\\", "\\")]
    ok(not left, f"no migration name or dependency left under {old}" + (f": {left[:3]}" if left else ""))

    moved = 0
    for name, (start, end) in before["old_migrations"].items():
        row = migrations.get(renamed(name, old_ns, new_ns))
        if row and row[1:] == (start, end):
            moved += 1
    ok(moved == len(before["old_migrations"]),
       f"{moved}/{len(before['old_migrations'])} old migrations kept their history under {new}, so none ran again")

    modules = board.sql("SELECT module_basename, module_auth FROM phpbb_modules")
    stale = [m for m in modules if old_ns in m[0].lstrip("\\") or f"ext_{old}" in m[1]]
    ok(not stale, f"ACP modules moved to {new}" + (f": {stale[:3]} still old" if stale else ""))
    ok(len(modules) == before["module_count"], f"no duplicate ACP modules ({before['module_count']} -> {len(modules)})")

    exts = dict(board.sql("SELECT ext_name, ext_active FROM phpbb_ext"))
    ok(old not in exts, f"old extension record removed")
    ok(exts.get(new) == 1, f"{new} enabled")

    config = dict(board.sql("SELECT config_name, config_value FROM phpbb_config"))
    missing = [k for k in before["old_config"] if k not in config]
    ok(not missing, f"{len(before['old_config'])} settings added by the old version kept" + (f"; missing: {missing}" if missing else ""))
    changed = [k for k, v in before["old_config"].items() if k in config and config[k] != v]
    if changed:
        print(f"  [note] settings changed by the new version's migrations: {', '.join(changed)}")
    marker = before.get("marker")
    if marker:
        ok(config.get(marker[0]) == marker[1], f"changed setting {marker[0]} kept its value")

    dotted = old.replace("/", ".") + "."
    types = [t for (t,) in board.sql("SELECT notification_type_name FROM phpbb_notification_types")
             if t.startswith(dotted)]
    types += [t for (t,) in board.sql("SELECT DISTINCT item_type FROM phpbb_user_notifications") if t.startswith(dotted)]
    ok(not types, f"no notification types left under {dotted}*" + (f": {types}" if types else ""))
    names = [k for k in config if dotted in k]
    ok(not names, f"no settings named after the old service names" + (f": {names}" if names else ""))


def check_pages(board: Board, new: str, old_module_count: int) -> None:
    """Load the board index, a topic and the extension's ACP pages as the admin."""
    session = requests.Session()
    sid = board.login(session, acp=True)
    for path in ("index.php", "viewtopic.php?t=1"):
        r = session.get(f"{board.base}/{path}", timeout=30)
        ok(r.status_code == 200 and "General Error" not in r.text, f"{path} -> HTTP {r.status_code}")
    new_ns = namespace(new)
    module_ids = [mid for mid, basename in board.sql(
        "SELECT module_id, module_basename FROM phpbb_modules WHERE module_mode <> ''")
        if basename.lstrip("\\").startswith(new_ns)]
    ok(len(module_ids) == old_module_count,
       f"{len(module_ids)} of the old version's {old_module_count} ACP pages found under {new}")
    for mid in module_ids:
        r = session.get(f"{board.base}/adm/index.php?sid={sid}&i={mid}", timeout=30)
        ok(r.status_code == 200 and "General Error" not in r.text and "Fatal" not in r.text,
           f"ACP module {mid} -> HTTP {r.status_code}")
    errors = board.php_errors()
    ok(not errors, "no PHP errors logged" + (f": {errors[0][:200]}" if errors else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="Check that a renamed extension upgrades cleanly from its old "
                                             "vendor name on a local test board.",
                                 epilog="Example: rename_upgrade.py ../stopforumspam@0af40a1 ../stopforumspam. "
                                        "Exit status: 0 all passed, 1 a check failed, 2 bad arguments.")
    ap.add_argument("old", metavar="OLD_REPO@REF",
                    help="git checkout and ref of the last version under the old name")
    ap.add_argument("new", metavar="NEW_REPO[@REF]",
                    help="git checkout of the renamed version, optionally with a git ref (default ref: HEAD)")
    ap.add_argument("-w", "--with", dest="needed", action="append", default=[], metavar="PATH[@REF]",
                    help="git checkout of an extension both versions require (default ref: HEAD); installed "
                         "and enabled first; repeat for several, in the order they must be enabled")
    ap.add_argument("-s", "--setting", metavar="NAME=VALUE",
                    help="change one of the old version's settings before the upgrade and check it survives, "
                         "for example sfs_api_key=UPGRADE-TEST")
    ap.add_argument("-b", "--board-dir",
                    help="board directory created by setup-board.sh (default: $PHPBB_TEST_BOARD)")
    ap.add_argument("-p", "--port", type=int, default=8083,
                    help="port for the temporary web server (default: 8083)")
    args = ap.parse_args()

    try:
        old_repo, old_ref = parse_spec(args.old)
        new_repo, new_ref = parse_spec(args.new)
        needed = [parse_spec(spec) for spec in args.needed]
        old, new = ext_name(old_repo, old_ref), ext_name(new_repo, new_ref)
    except ValueError as e:
        ap.error(str(e))
    if old == new:
        ap.error(f"both versions are named {old}; this script tests a change of name")
    marker = None
    if args.setting:
        name, sep, value = args.setting.partition("=")
        if not sep or not name:
            ap.error("--setting must look like NAME=VALUE")
        marker = (name, value)

    board = Board(args.board_dir, args.port)
    print(f"== {old} ({old_repo.name} @ {old_ref}) -> {new} ({new_repo.name} @ {new_ref})")
    board.reset()
    board.installed = []
    try:
        for repo, ref in needed:
            dep, _ = board._copy_ext(repo, ref)
            if "Successfully" not in enable(board, dep):
                ok(False, f"needed extension {dep} enabled")
                return 1

        # 1. The old version, and what it added
        config_before = dict(board.sql("SELECT config_name, config_value FROM phpbb_config"))
        _, old_target = board._copy_ext(old_repo, old_ref)
        ok("Successfully" in enable(board, old), f"old version {old} enabled")
        if marker:
            board.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (marker[1], marker[0]))
            board.purge_cache()
        old_ns = namespace(old)
        before = {
            "old_migrations": {name: (start, end) for name, start, end in board.sql(
                "SELECT migration_name, migration_start_time, migration_end_time FROM phpbb_migrations")
                if name.lstrip("\\").startswith(old_ns)},
            "module_count": board.sql("SELECT COUNT(*) FROM phpbb_modules")[0][0],
            "old_module_pages": len([1 for (basename,) in board.sql(
                "SELECT module_basename FROM phpbb_modules WHERE module_mode <> ''")
                if basename.lstrip("\\").startswith(old_ns)]),
            "old_config": {k: v for k, v in board.sql("SELECT config_name, config_value FROM phpbb_config")
                           if k not in config_before},
            "marker": marker,
        }
        ok(before["old_migrations"], f"old version installed {len(before['old_migrations'])} migrations")
        if marker:
            ok(marker[0] in before["old_config"], f"--setting {marker[0]} is one of the old version's settings")

        # 2. The new version must wait for the old one to be disabled
        board._copy_ext(new_repo, new_ref)
        out = enable(board, new)
        ok("Successfully" not in out and old in out,
           "new version refuses to enable while the old one is enabled, naming it"
           + ("" if old in out else f": {out.strip()[-200:]}"))

        # 3. The upgrade itself
        out = board.cli("extension:disable", old)
        board.purge_cache()
        ok("Successfully" in out, "old version disabled")
        out = enable(board, new)
        ok("Successfully" in out, "new version enabled" + ("" if "Successfully" in out else f": {out.strip()[-200:]}"))
        # Admins delete the old files once the new version runs
        shutil.rmtree(old_target)
        if not any(old_target.parent.iterdir()):
            old_target.parent.rmdir()
        board.purge_cache()

        # 4. and 5.
        check_database(board, old, new, before)
        with board.serve():
            check_pages(board, new, before["old_module_pages"])
    finally:
        # Removes every extension this run copied in and restores the board
        if board.installed:
            board.remove_ext(board.installed[-1])
        else:
            board.reset()

    failed = [label for good, label in results if not good]
    print(f"== {len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
