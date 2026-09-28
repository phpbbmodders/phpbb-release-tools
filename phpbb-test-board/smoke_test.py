#!/usr/bin/env python3
"""
Smoke-test a phpBB extension on a local test board.

Installs the extension from a git checkout into a clean copy of the board
(created by setup-board.sh), after any extensions it needs given with
--with, serves the board with PHP error logging on,
and loads:
  - core board pages, as a guest and as the admin
  - the extension's routes that need no parameters
  - its ACP, MCP and UCP modules, in every mode
  - its cron tasks (a task with no name is reported: that crashes pages)
then runs a disable -> delete data -> enable round trip.

Any HTTP 5xx, empty page, "[phpBB Debug]" notice, "General Error", "Fatal
error" or PHP error-log entry is reported. The board is restored afterwards.

Exit status: 0 if everything was clean, 1 if a problem was found, 2 for bad
arguments.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

import requests  # noqa: E402
import yaml  # noqa: E402

from _board import Board, ext_name, parse_spec, required_packages  # noqa: E402

CORE_PAGES = ["index.php", "viewforum.php?f=2", "viewtopic.php?t=1", "memberlist.php",
              "memberlist.php?mode=viewprofile&u=2", "posting.php?mode=post&f=2",
              "search.php", "ucp.php", "faq.php"]
SCRIPTS = {"acp": "adm/index.php", "mcp": "mcp.php", "ucp": "ucp.php"}
CRON_TASKS_PHP = ("define('IN_PHPBB',1);$phpbb_root_path='./';$phpEx='php';require 'common.php';"
                  "$m=$phpbb_container->get('cron.manager');"
                  "$r=new ReflectionMethod($m,'load_tasks_from_container');$r->setAccessible(true);$r->invoke($m);"
                  "$p=new ReflectionProperty($m,'tasks');$p->setAccessible(true);"
                  "foreach($p->getValue($m) as $t){$w=new ReflectionProperty($t,'task');$w->setAccessible(true);"
                  "echo get_class($w->getValue($t)),'|',$t->get_name(),PHP_EOL;}")


class Report:
    """Collects and prints check results."""

    def __init__(self) -> None:
        self.problems: list = []

    def ok(self, label: str) -> None:
        print(f"  [ok] {label}")

    def problem(self, label: str) -> None:
        print(f"  [PROBLEM] {label}")
        self.problems.append(label)

    def response(self, label: str, resp: requests.Response) -> None:
        """Check one HTTP response for errors."""
        body = resp.text or ""
        issues = []
        if resp.status_code >= 500:
            issues.append(f"HTTP {resp.status_code}")
        if not body.strip():
            issues.append("empty page")
        for pattern, name in ((r"\[phpBB Debug\][^<]*<[^>]*>[^<]*", "debug notice"),
                              (r"General Error", "General Error"), (r"Fatal error", "Fatal error")):
            m = re.search(pattern, body)
            if m:
                issues.append(f"{name}: {re.sub('<[^>]+>', '', m.group(0))[:160]}")
        if issues:
            self.problem(f"{label} -> {resp.status_code}: {'; '.join(issues)}")
        else:
            self.ok(f"{label} -> {resp.status_code}")


def check_routes(board: Board, report: Report, target: Path, sessions: dict) -> None:
    """Load every extension route that needs no parameters."""
    routing = target / "config/routing.yml"
    if not routing.exists():
        return
    for name, route in (yaml.safe_load(routing.read_text()) or {}).items():
        route = route or {}
        path = route.get("path", "")
        defaults = route.get("defaults", {}) or {}
        params = re.findall(r"\{(\w+)\}", path)
        if not all(p in defaults for p in params):
            print(f"  [skip] route {name} {path} (needs parameters)")
            continue
        path = re.sub(r"\{(\w+)\}", lambda m: str(defaults[m.group(1)]), path)
        for who, s in sessions.items():
            report.response(f"{who} route {name} app.php{path}", s.get(f"{board.base}/app.php{path}", timeout=60))


def check_modules(board: Board, report: Report, ns: str, admin: requests.Session, sid: str) -> None:
    """Load the extension's ACP/MCP/UCP modules in every mode."""
    rows = board.sql("SELECT module_class, module_id, module_basename, module_mode FROM phpbb_modules "
                     "WHERE module_mode <> '' ORDER BY module_class, module_id")
    for cls, mid, basename, mode in rows:
        if basename.lstrip("\\").startswith(ns):
            url = f"{board.base}/{SCRIPTS[cls]}?i={mid}&mode={mode}&sid={sid}"
            report.response(f"admin {cls.upper()} {basename.rsplit(chr(92), 1)[-1]} mode={mode}",
                            admin.get(url, timeout=60))


def check_cron(board: Board, report: Report, ns: str, admin: requests.Session) -> None:
    """Trigger the extension's cron tasks; report tasks with no name."""
    out = subprocess.run(["php", "-d", "opcache.enable_cli=0", "-r", CRON_TASKS_PHP],
                         cwd=board.root, capture_output=True, text=True).stdout
    for line in out.splitlines():
        cls, _, name = line.partition("|")
        if not cls.startswith(ns):
            continue
        if not name:
            report.problem(f"cron task {cls} has no name (its service is missing set_name)")
            continue
        report.response(f"cron {name}", admin.get(f"{board.base}/app.php/cron/{name}", timeout=60))


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test a phpBB extension on a local test board.",
                                 epilog="Exit status: 0 clean, 1 problems found, 2 bad arguments.")
    ap.add_argument("repo", help="path to the extension's git checkout")
    ap.add_argument("-r", "--ref", default="HEAD",
                    help="git ref to install, for example origin/main (default: HEAD)")
    ap.add_argument("-w", "--with", dest="needed", action="append", default=[], metavar="PATH[@REF]",
                    help="git checkout of an extension this one needs, installed and enabled first "
                         "(default ref: HEAD); repeat for several, in the order they must be enabled")
    ap.add_argument("-b", "--board-dir",
                    help="board directory created by setup-board.sh (default: $PHPBB_TEST_BOARD)")
    ap.add_argument("-p", "--port", type=int, default=8083,
                    help="port for the temporary web server (default: 8083)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        ap.error(f"{repo} is not a git checkout")
    try:
        needed = [parse_spec(spec) for spec in args.needed]
    except ValueError as e:
        ap.error(f"--with: {e}")
    board = Board(args.board_dir, args.port)
    report = Report()

    supplied = {ext_name(r, ref) for r, ref in needed}
    for package in required_packages(repo, args.ref):
        if package not in supplied:
            print(f"  [note] composer.json requires {package}; if it is a phpBB extension, add --with PATH")

    ext, target, out = board.install_ext(repo, args.ref, needed)
    vendor, name = ext.split("/")
    ns = f"{vendor}\\{name}\\"
    print(f"== {ext} ({repo.name} @ {args.ref})")
    try:
        if "Successfully" not in out:
            report.problem(f"enable failed: {out.strip()[-300:]}")
            return finish(report)
        report.ok("enabled")

        with board.serve():
            guest, admin = requests.Session(), requests.Session()
            for page in CORE_PAGES:
                report.response(f"guest {page}", guest.get(f"{board.base}/{page}", timeout=60))
            sid = board.login(admin, acp=True)
            for page in CORE_PAGES:
                report.response(f"admin {page}", admin.get(f"{board.base}/{page}", timeout=60))
            report.response("admin adm/index.php", admin.get(f"{board.base}/adm/index.php?sid={sid}", timeout=60))
            check_routes(board, report, target, {"guest": guest, "admin": admin})
            check_modules(board, report, ns, admin, sid)
            check_cron(board, report, ns, admin)
        for entry in board.php_errors()[:10]:
            report.problem(f"php log: {entry[:300]}")

        for step in ("disable", "purge", "enable"):
            out = board.cli(f"extension:{step}", ext)
            if "Successfully" not in out:
                report.problem(f"{step} failed: {out.strip()[-300:]}")
                break
        else:
            report.ok("disable -> delete data -> enable round trip")
        return finish(report)
    finally:
        board.remove_ext(target)


def finish(report: Report) -> int:
    """Print the summary and return the exit status."""
    print(f"== result: {'CLEAN' if not report.problems else f'{len(report.problems)} problem(s)'}")
    for p in report.problems:
        print(f"   - {p}")
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
