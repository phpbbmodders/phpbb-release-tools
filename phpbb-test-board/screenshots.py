#!/usr/bin/env python3
"""
Take documentation screenshots of a phpbbmodders extension or style on a local test board.

Installs the extension or style from git into a clean copy of the board
(created by setup-board.sh), renames the board to "Example board", serves
it, and saves PNG screenshots with Playwright (Chromium, English). An
extension is shown in prosilver, with its board and ACP pages; a style is
made every user's style and shown on board pages. The pages are listed in
SHOTS below, keyed by an extension's composer name or a style's name from
style.cfg; add an entry for a new project. Restores the board afterwards.

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

from _board import Board, ext_name, is_style, parse_spec, required_packages, style_cfg  # noqa: E402

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


# -- screenshots, one function per project ----------------------------------

def busiest_forum_and_topic(board: Board) -> tuple:
    """The forum with the most topics and the topic with the most replies, so pages show real content."""
    forum = board.sql("SELECT forum_id FROM phpbb_forums WHERE forum_type = 1 "
                      "ORDER BY forum_topics_approved DESC, forum_id LIMIT 1")[0][0]
    topic = board.sql("SELECT topic_id FROM phpbb_topics "
                      "ORDER BY topic_posts_approved DESC, topic_id LIMIT 1")[0][0]
    return forum, topic


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


def shots_prominodeux(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """ProMinoDeux style: the main board pages, on desktop and phone. Use --seed for real content."""
    forum, topic = busiest_forum_and_topic(board)
    shoot.shot("prominodeux-index", "index.php")
    shoot.shot("prominodeux-viewforum", f"viewforum.php?f={forum}")
    shoot.shot("prominodeux-viewtopic", f"viewtopic.php?t={topic}")
    shoot.shot("prominodeux-posting", f"posting.php?mode=reply&t={topic}&sid={{sid}}", admin=True)
    shoot.shot("prominodeux-index-phone", "index.php", phone=True)
    shoot.shot("prominodeux-viewtopic-phone", f"viewtopic.php?t={topic}", phone=True)


# Made-up values for the StopForumSpam screenshots. Nothing is ever sent to
# stopforumspam.com: the pages shown only read the board's own data and logs.
SFS_EXAMPLE_API_KEY = "0123456789abcdef"
SFS_EXAMPLE_POSTER_IP = "93.184.216.34"  # a public address, so posts can be reported


def enable_stopforumspam(board: Board) -> None:
    """Turn StopForumSpam on with an example API key, and give posts a public poster IP."""
    for name, value in (("allow_sfs", "1"), ("sfs_api_key", SFS_EXAMPLE_API_KEY)):
        board.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, name))
    board.sql("UPDATE phpbb_posts SET poster_ip = ?", (SFS_EXAMPLE_POSTER_IP,))
    board.purge_cache()
    # The admins-and-moderators list the ACP asks for when an API key is set;
    # built locally from the board's own groups, so staff can't be reported.
    board.php("$phpbb_container->get('phpbbmodders.stopforumspam.core.sfsgroups')->build_adminsmods_cache();", check=True)


def shots_stopforumspam(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/stopforumspam: its ACP settings and the report button on posts. Use --seed."""
    enable_stopforumspam(board)
    shoot.shot("stopforumspam-acp-settings",
               "adm/index.php?i=-phpbbmodders-stopforumspam-acp-stopforumspam_module&mode=settings&sid={sid}",
               admin=True)
    _, topic = busiest_forum_and_topic(board)
    def highlight_button(p) -> None:
        p.add_style_tag(content="li[id^='sfs'] .button { outline: 3px solid #d31141; outline-offset: 2px; }")
    shoot.shot("stopforumspam-report-button", f"viewtopic.php?t={topic}&sid={{sid}}", admin=True,
               selector=".post:has(li[id^='sfs'])", prepare=highlight_button)


def shots_sfscompanion(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/sfscompanion: its ACP logs and settings, and the profile link.

    Needs --with phpbbmodders/stopforumspam; use --seed. The lookup and scan
    pages are left out because opening them queries stopforumspam.com.
    """
    enable_stopforumspam(board)
    # Example log entries in the shape phpbbmodders/stopforumspam writes them:
    # logged by the guest (user 1) who was registering or posting.
    entries = (
        ("user", "203.0.113.9", "LOG_SFS_MESSAGE", "['reportee_id' => 1, 'casino_deals', '203.0.113.9', 'deals@example.com']"),
        ("user", "203.0.113.24", "LOG_SFS_MESSAGE", "['reportee_id' => 1, 'cheap_pills_now', '203.0.113.24', 'pills@example.net']"),
        ("user", "198.51.100.41", "LOG_SFS_MESSAGE", "['reportee_id' => 1, 'seo_expert_99', '198.51.100.41', 'seo@example.org']"),
        ("user", "192.0.2.77", "LOG_SFS_MESSAGE", "['reportee_id' => 1, 'free_followers', '192.0.2.77', 'followers@example.com']"),
        ("admin", "198.51.100.7", "LOG_SFS_DOWN", "[]"),
        ("admin", "198.51.100.8", "LOG_SFS_DOWN", "[]"),
    )
    board.php("".join(f"$phpbb_log->add('{mode}', 1, '{ip}', '{op}', false, {data});"
                      for mode, ip, op, data in entries), check=True)
    board.sql("UPDATE phpbb_config SET config_value = '30' WHERE config_name = 'sfsc_expire_days'")
    board.purge_cache()
    page = "adm/index.php?i=-phpbbmodders-sfscompanion-acp-main_module&sid={sid}&mode="
    shoot.shot("sfscompanion-acp-blocks", page + "blocks", admin=True)
    shoot.shot("sfscompanion-acp-errors", page + "errors", admin=True)
    shoot.shot("sfscompanion-acp-settings", page + "settings", admin=True)
    member = board.sql("SELECT user_id FROM phpbb_users WHERE user_type = 0 AND user_id <> 2 "
                       "ORDER BY user_posts DESC, user_id LIMIT 1")
    member_id = member[0][0] if member else 2
    def highlight_link(p) -> None:
        p.add_style_tag(content="a[href*='sfs-companion/finder'] { background: #ffd54f; border-radius: 3px; padding: 2px 3px; }")
    shoot.shot("sfscompanion-profile-link", f"memberlist.php?mode=viewprofile&u={member_id}&sid={{sid}}",
               admin=True, selector=".panel:has(a[href*='sfs-companion/finder'])", prepare=highlight_link)


SHOTS = {
    "phpbbmodders/documentation": shots_documentation,
    "phpbbmodders/sfscompanion": shots_sfscompanion,
    "phpbbmodders/stopforumspam": shots_stopforumspam,
    "ProMinoDeux": shots_prominodeux,
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
    ap = argparse.ArgumentParser(description="Take documentation screenshots of a phpbbmodders extension or style "
                                             "on a local test board.",
                                 epilog="Projects with screenshots: " + ", ".join(sorted(SHOTS))
                                        + ". Exit status: 0 all saved, 1 a screenshot failed, 2 bad arguments.")
    ap.add_argument("repo", metavar="REPO[@REF]",
                    help="extension or style git checkout, optionally with a git ref to install "
                         "(default ref: HEAD); a style is recognised by its style.cfg")
    ap.add_argument("-o", "--out", required=True, metavar="DIR",
                    help="directory to save the PNG files in, for example the project's docs/images")
    ap.add_argument("-s", "--seed", metavar="SCRIPT",
                    help="PHP script that fills the board with forums, topics and users after the install, "
                         "run as 'php SCRIPT BOARD_ROOT'; for example seed-forum's bin/seed-standard-fixtures.php")
    ap.add_argument("--build", metavar="DIR",
                    help="phpbbdocs-hugo build to serve (phpbbmodders/documentation only)")
    ap.add_argument("-w", "--with", dest="needed", action="append", default=[], metavar="PATH[@REF]",
                    help="git checkout of an extension the extension requires (default ref: HEAD); "
                         "installed and enabled first; repeat for several, in the order they must be enabled "
                         "(extensions only)")
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
    if args.seed and not Path(args.seed).is_file():
        ap.error(f"--seed {args.seed} is not a file")
    if args.build and not (Path(args.build) / "index.html").is_file():
        ap.error(f"--build {args.build} is not a built site (no index.html)")
    style = is_style(repo, ref)
    if style and needed:
        ap.error("--with is for extensions; a style needs no other extensions")
    name = style_cfg(repo, ref)["name"] if style else ext_name(repo, ref)
    if name not in SHOTS:
        ap.error(f"no screenshots defined for {name}; add them to SHOTS in {Path(__file__).name}")
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    board = Board(args.board_dir, args.port)
    if style:
        ext, target = board.install_style(repo, ref)
        out = "Successfully installed"
    else:
        for package in required_packages(repo, ref):
            if package not in {ext_name(r, dep_ref) for r, dep_ref in needed}:
                print(f"  [note] {repo.name} requires {package}; if it is a phpBB extension, add --with PATH")
        ext, target, out = board.install_ext(repo, ref, needed)
    print(f"== {ext} ({repo.name} @ {ref}) -> {out_dir}")
    try:
        if "Successfully" not in out:
            print(f"  [FAIL] enable: {out.strip()[-200:]}")
            return 1
        if args.seed:
            seeded = subprocess.run(["php", "-d", "opcache.enable_cli=0", str(Path(args.seed).resolve()),
                                     str(board.root)], capture_output=True, text=True)
            if seeded.returncode != 0:
                print(f"  [FAIL] seed: {(seeded.stderr or seeded.stdout).strip()[-300:]}")
                return 1
            print(f"  [ok] seeded: {seeded.stdout.strip().splitlines()[-1][:200] if seeded.stdout.strip() else 'done'}")
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
        if style:
            board.remove_style(target)
        else:
            board.remove_ext(target)


if __name__ == "__main__":
    sys.exit(main())
