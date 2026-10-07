#!/usr/bin/env python3
"""
Take documentation screenshots of a phpbbmodders extension on a local test board.

Installs the extension from git into a clean copy of the board (created by
setup-board.sh), renames the board to "Example board", serves it, and saves
PNG screenshots of the extension's board pages and ACP pages with Playwright
(Chromium, prosilver, English). The pages for each extension are listed in
SHOTS below, keyed by composer name; add an entry for a new extension.
Restores the board afterwards.

The first run installs Playwright's Chromium into the user's cache.

Exit status: 0 if every screenshot was saved, 1 if any failed, 2 for bad arguments.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

import requests  # noqa: E402
from playwright.sync_api import Browser, Error as PlaywrightError, sync_playwright  # noqa: E402

from _board import Board, ext_name, parse_spec, required_packages  # noqa: E402

BOARD_NAME = "Example board"
BOARD_DESCRIPTION = "A phpBB board"
DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}
TIMEOUT_MS = 30000


class Shooter:
    """Saves screenshots of board pages as a guest or as the admin."""

    def __init__(self, board: Board, browser: Browser, out_dir: Path) -> None:
        self.board = board
        self.browser = browser
        self.out_dir = out_dir
        self.saved: list = []
        self.failed: list = []
        self.cleanup: list = []  # paths to delete once the screenshots are taken
        self._admin_sid = ""
        self._admin_cookies: list = []
        self._admin_agent = ""

    def admin_sid(self) -> str:
        """Log the admin in, ACP included, once; return the session id."""
        if not self._admin_sid:
            session = requests.Session()
            self._admin_sid = self.board.login(session, acp=True)
            # phpBB checks the browser string against the one that logged in.
            self._admin_agent = session.headers["User-Agent"]
            self._admin_cookies = [{"name": c.name, "value": c.value, "domain": "localhost", "path": "/"}
                                   for c in session.cookies]
        return self._admin_sid

    def shot(self, name: str, url: str, admin: bool = False, phone: bool = False,
             selector: str = "", prepare: Callable = None) -> None:
        """Save one screenshot as OUT_DIR/NAME.png.

        url is relative to the board root. With admin=True the admin's session
        is used and {sid} in url is replaced by its session id. selector crops
        the image to that element; prepare(page) runs before the capture, for
        example to open a tab or wait for results loaded by JavaScript.
        """
        if admin:
            url = url.replace("{sid}", self.admin_sid())
        options = {"viewport": PHONE if phone else DESKTOP, "locale": "en-GB",
                   "device_scale_factor": 1, "is_mobile": phone, "has_touch": phone}
        if admin:
            options["user_agent"] = self._admin_agent
        context = self.browser.new_context(**options)
        if admin:
            context.add_cookies(self._admin_cookies)
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        target = self.out_dir / f"{name}.png"
        try:
            response = page.goto(f"{self.board.base}/{url}", wait_until="networkidle")
            if response is None or response.status >= 400:
                raise RuntimeError(f"HTTP {response.status if response else 'no response'}")
            if prepare:
                prepare(page)
            if selector:
                page.locator(selector).first.screenshot(path=str(target))
            else:
                page.screenshot(path=str(target))
            self.saved.append(target)
            print(f"  [ok] {target.name}")
        except (PlaywrightError, RuntimeError) as e:
            self.failed.append(name)
            print(f"  [FAIL] {name}: {str(e).splitlines()[0][:200]}")
        finally:
            context.close()


# -- screenshots, one function per extension --------------------------------

def shots_documentation(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/documentation: needs --build, a phpbbdocs-hugo build."""
    if not args.build:
        raise SystemExit("error: phpbbmodders/documentation needs --build DIR, a phpbbdocs-hugo build")
    path = board.sql("SELECT config_value FROM phpbb_config "
                     "WHERE config_name = 'phpbbmodders_documentation_docs_path'")[0][0]
    build = board.root / path
    shutil.rmtree(build, ignore_errors=True)
    shutil.copytree(args.build, build)
    shoot.cleanup.append(build)
    requests.get(f"{board.base}/index.php")  # runs the permission sync left pending by the install
    board.purge_cache()

    page = "app.php/documentation/en/userguide/user_permissions"
    shoot.shot("documentation-page", page)
    def show_article(p) -> None:
        # On a phone the sidebar comes first; show the article instead.
        p.locator(".documentation-main").scroll_into_view_if_needed()
    shoot.shot("documentation-page-phone", page, phone=True, prepare=show_article)
    shoot.shot("documentation-navbar", "index.php", selector="#page-header")

    def wait_for_results(p) -> None:
        p.wait_for_selector(".doc-search-results li")
    shoot.shot("documentation-search", "app.php/documentation-search/en?q=permissions&js=1",
               prepare=wait_for_results)

    shoot.shot("documentation-acp-settings",
               "adm/index.php?i=-phpbbmodders-documentation-acp-main_module&mode=settings&sid={sid}",
               admin=True)

    guests = board.sql("SELECT group_id FROM phpbb_groups WHERE group_name = 'GUESTS'")[0][0]

    def open_misc_tab(p) -> None:
        # Advanced permissions, Misc category, where the documentation permissions are listed.
        p.locator("a[onclick*='swap_options'], a:has-text('Advanced Permissions')").first.click()
        p.locator("li[id^='tab'] a:has-text('Misc'), a:has-text('Misc')").first.click()
    shoot.shot("documentation-acp-permissions",
               f"adm/index.php?i=acp_permissions&mode=setting_group_global&group_id[0]={guests}&type=u_&sid={{sid}}",
               admin=True, prepare=open_misc_tab, selector="fieldset:has(legend:has-text('Guests'))")


SHOTS = {
    "phpbbmodders/documentation": shots_documentation,
}


def ensure_chromium(playwright) -> Browser:
    """Launch Chromium, installing Playwright's copy first if it is missing."""
    try:
        return playwright.chromium.launch()
    except PlaywrightError:
        print("Installing Playwright's Chromium ...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        return playwright.chromium.launch()


def main() -> int:
    ap = argparse.ArgumentParser(description="Take documentation screenshots of a phpbbmodders extension "
                                             "on a local test board.",
                                 epilog="Extensions with screenshots: " + ", ".join(sorted(SHOTS))
                                        + ". Exit status: 0 all saved, 1 a screenshot failed, 2 bad arguments.")
    ap.add_argument("repo", metavar="REPO[@REF]",
                    help="extension git checkout, optionally with a git ref to install (default ref: HEAD)")
    ap.add_argument("-o", "--out", required=True, metavar="DIR",
                    help="directory to save the PNG files in, for example the extension's docs/images")
    ap.add_argument("--build", metavar="DIR",
                    help="phpbbdocs-hugo build to serve (phpbbmodders/documentation only)")
    ap.add_argument("-w", "--with", dest="needed", action="append", default=[], metavar="PATH[@REF]",
                    help="git checkout of an extension the extension requires (default ref: HEAD); "
                         "installed and enabled first; repeat for several, in the order they must be enabled")
    ap.add_argument("-b", "--board-dir",
                    help="board directory created by setup-board.sh (default: $PHPBB_TEST_BOARD)")
    ap.add_argument("-p", "--port", type=int, default=8083,
                    help="port for the temporary web server (default: 8083)")
    args = ap.parse_args()

    try:
        repo, ref = parse_spec(args.repo)
        needed = [parse_spec(spec) for spec in args.needed]
    except ValueError as e:
        ap.error(str(e))
    if args.build and not (Path(args.build) / "index.html").is_file():
        ap.error(f"--build {args.build} is not a built site (no index.html)")
    name = ext_name(repo, ref)
    if name not in SHOTS:
        ap.error(f"no screenshots defined for {name}; add them to SHOTS in {Path(__file__).name}")
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    board = Board(args.board_dir, args.port)
    for package in required_packages(repo, ref):
        if package not in {ext_name(r, dep_ref) for r, dep_ref in needed}:
            print(f"  [note] {repo.name} requires {package}; if it is a phpBB extension, add --with PATH")
    ext, target, out = board.install_ext(repo, ref, needed)
    print(f"== {ext} ({repo.name} @ {ref}) -> {out_dir}")
    try:
        if "Successfully" not in out:
            print(f"  [FAIL] enable: {out.strip()[-200:]}")
            return 1
        board.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = 'sitename'", (BOARD_NAME,))
        board.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = 'site_desc'", (BOARD_DESCRIPTION,))
        board.purge_cache()
        with board.serve(), sync_playwright() as playwright:
            browser = ensure_chromium(playwright)
            shooter = Shooter(board, browser, out_dir)
            try:
                SHOTS[ext](board, shooter, args)
            finally:
                browser.close()
                for path in shooter.cleanup:
                    shutil.rmtree(path, ignore_errors=True)
        errors = board.php_errors()
        if errors:
            print(f"  [FAIL] PHP errors logged: {errors[0][:200]}")
        print(f"== {len(shooter.saved)} saved, {len(shooter.failed)} failed")
        return 1 if shooter.failed or errors else 0
    finally:
        board.remove_ext(target)


if __name__ == "__main__":
    sys.exit(main())
