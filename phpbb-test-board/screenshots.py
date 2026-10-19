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
import json
import re
import shutil
import subprocess
import sys
import time
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
             selector: str = "", prepare: Callable = None, full_page: bool = False) -> None:
        """Save one screenshot as OUT_DIR/NAME.png.

        url is relative to the board root. With admin=True the admin's session
        is used and {sid} in url is replaced by its session id. selector crops
        the image to that element; prepare(page) runs before the capture, for
        example to open a tab or wait for results loaded by JavaScript.
        full_page captures the whole page instead of the visible window.
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
                page.screenshot(path=str(target), full_page=full_page)
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


def php_string(text: str) -> str:
    """A PHP single-quoted string literal holding text exactly: no $ expansion, Unicode kept as is."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def shots_wiki(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/wiki: overview, articles, editing, approval, links between articles, moving, the
    lists of templates and categories, an infobox and navbox, the ACP Starter templates page, the notice
    and the lock for two people editing one article, and per-article edit protection."""
    def add_article(title: str, text: str, description: str = "", views: int = 0, sticky: int = 0,
                    approved: int = 1, days_ago: float = 0, edit_reason: str = "") -> int:
        """Store one article version as the edit form does: the text escaped as phpBB's request
        escapes what is typed (so an entity such as &#32; arrives as written), then parsed with
        the wiki's markup on, so categories and templates are read; return its id."""
        out = board.php(
            "$t = " + php_string(text) + "; $uid = $bit = ''; $flags = 0; $typed = $t;"
            "(new \\phpbb\\request\\type_cast_helper())->set_var($t, $typed, 'string', true);"
            "\\phpbbmodders\\wiki\\wiki\\markup::set_active(true);"
            "generate_text_for_storage($t, $uid, $bit, $flags, true, true, true);"
            "\\phpbbmodders\\wiki\\wiki\\markup::set_active(false);"
            "$when = time() - " + str(int(days_ago * 86400)) + ";"
            "$db->sql_query('INSERT INTO ' . $table_prefix . 'wiki_article ' . "
            "$db->sql_build_array('INSERT', ['article_title' => " + php_string(title) + ", 'article_url' => "
            + php_string(title) + ", 'article_text' => $t, 'bbcode_uid' => $uid, 'bbcode_bitfield' => $bit, "
            "'article_approved' => " + str(approved) + ", 'article_user_id' => 2, 'article_last_edit' => $when, "
            "'article_time_created' => $when, 'article_sources' => '', 'article_description' => "
            + php_string(description) + ", 'article_views' => " + str(views) + ", 'article_sticky' => "
            + str(sticky) + ", 'article_edit_reason' => " + php_string(edit_reason) + "]));"
            "echo $db->sql_nextid();",
            check=True)
        return int(out.strip().splitlines()[-1])

    add_article("Getting Started",
                "Welcome to the community wiki. Anyone can read it, and members can help keep it up to date."
                "\n\n[b]Where to begin[/b]\n[list]\n[*]New to the board? Read the [[Frequently Asked Questions]]."
                "\n[*]Installing the software yourself? Start with [[Installation]].\n[*]Writing posts? The "
                "[[Formatting Guide]] covers bold text, lists, links and code.\n[/list]\n\nEvery edit is kept, "
                "so nothing is lost: open [i]Versions[/i] on any article to see who changed what.",
                "A quick tour of the board and the wiki.", views=148, sticky=1, days_ago=12)
    add_article("Installation",
                "Before you start, read the [[Requirements]]. Then follow [[Upgrading]] if you already run an "
                "older version, or see the [[Troubleshooting|troubleshooting tips]] if something goes wrong."
                "\n\nOnce installed, the [[Configuration]] page explains every setting.",
                "Installing the software step by step.", views=96, days_ago=9)
    add_article("Requirements", "You need phpBB 3.3 and PHP 7.4 or later. See [[Installation]] for the next steps."
                "\n\n[[Category:Setup]]", "What your server needs.", views=64, days_ago=8)
    # A template, used in an article, for the list of templates; and a category with a page
    add_article("Template:Out of date",
                "<noinclude>Shows a notice that an article needs updating. Write it at the top of the "
                "article:\n\n{{Out of date|reason=the 2.0 release changed this}}</noinclude>"
                "<table><tr><td>[b]This article is out of date.[/b]{{#if:{{{reason|}}}|&#32;Reason: {{{reason}}}.}}"
                "</td></tr></table>",
                "A notice for articles that need updating", days_ago=7)
    add_article("Template:Start Guide",
                "<noinclude>A starting point for a new guide.</noinclude>== Before you start ==\n\n"
                "== Steps ==\n\n== See also ==",
                "A starting point for a guide", days_ago=7)
    add_article("Configuration", "{{Out of date|reason=the 2.0 release added settings}}\n\nEvery setting is "
                "described here. Return to [[Installation]].\n\n[[Category:Setup]]",
                "Every setting explained.", views=51, days_ago=6)
    add_article("Category:Setup", "Installing the software and getting it ready.",
                "Installing and configuring the software", days_ago=6)
    add_article("Frequently Asked Questions",
                "[b]How do I change my avatar?[/b]\nOpen the User Control Panel and choose Profile."
                "\n\n[[Category:Guides]]",
                "Common questions from new members.", views=23, days_ago=4)
    guide = ("Use [b]bold[/b] for emphasis, [i]italic[/i] for titles and [code]code[/code] for code."
             "\n\nSee the [[Getting Started]] page for more help.\n\n[[Category:Guides]]")
    active = add_article("Formatting Guide", guide, "How to format posts with BBCode.", views=37, days_ago=3)
    pending = add_article("Formatting Guide",
                          guide + "\n\nTo quote another post, click [i]Quote[/i] below it.",
                          "How to format posts with BBCode.", views=37, approved=0, days_ago=0.1,
                          edit_reason="Added a note about quoting")
    # An infobox, a navbox and centred text, as the starter templates draw them
    infobox = ('<table class="infobox"><caption>{{{title|}}}</caption>'
               '{{#if:{{{above|}}}|<tr><th colspan="2">{{{above}}}</th></tr>}}'
               + "".join('{{#if:{{{data%d|}}}|<tr><th>{{{label%d}}}</th><td>{{{data%d}}}</td></tr>}}' % (i, i, i)
                         for i in range(1, 6))
               + '</table>')
    def starter_file(name: str, text: str) -> str:
        """A starter template file: the attribution header the Starter templates page asks for, then text."""
        slug = name.replace(" ", "_")
        return ("<noinclude>\nSource: https://en.wikipedia.org/wiki/Template:" + slug + "\n"
                "Adapted by phpBB Modders.\nChanges: rewritten for the wiki.\n"
                "Licence: CC BY-SA 4.0, https://creativecommons.org/licenses/by-sa/4.0/\n</noinclude>" + text)
    # As if installed from the starter templates, so its file below shows as already installed
    infobox_page = starter_file("Infobox", infobox)
    add_article("Template:Infobox", infobox_page, "A box of facts at the right of an article", days_ago=5)
    navbox = ('<details class="navbox" open><summary>{{{title|}}}</summary><table>'
              + "".join('{{#if:{{{list%d|}}}|<tr><th>{{{group%d}}}</th><td>{{{list%d}}}</td></tr>}}' % (i, i, i)
                        for i in range(1, 4))
              + '</table></details>')
    add_article("Template:Navbox", "<noinclude>Links to related articles, in a box the reader can close."
                "</noinclude>" + navbox, "Links to related articles", days_ago=5)
    add_article("Wiki extension",
                "{{Infobox|title=Wiki|above=phpBB extension|label1=Version|data1=1.1.0-a2-dev|label2=Requires"
                "|data2=phpBB 3.3.19 or later|label3=Licence|data3=GPL-2.0|label4=Developer|data4=phpBB Modders}}"
                "The [b]Wiki[/b] extension adds a wiki to a phpBB board. Members write and edit articles, every "
                "edit is kept as a version, and moderators approve changes before they go live. Articles link to "
                "each other with [[Getting Started|double square brackets]], and templates such as the box on "
                "the right keep repeated content in one place.\n\n<center>[i]Every article can be changed, "
                "compared and restored.[/i]</center>\n\n{{Navbox|title=Wiki help|group1=Start|list1=[[Getting "
                "Started]] · [[Installation]] · [[Requirements]]|group2=Writing|list2=[[Formatting Guide]] · "
                "[[Frequently Asked Questions]]|group3=Settings|list3=[[Configuration]]}}",
                "What the wiki extension does.", views=12, days_ago=2)

    # Template files for the Starter templates page: two new, one the board already has, and one
    # whose page was edited on the board, so the list shows each kind of row
    files = {
        "Main": starter_file("Main", "[i]Main article: [[{{{1}}}]][/i]"),
        "Stub": starter_file("Stub", "<table><tr><td>[i]This article is a stub.[/i]</td></tr></table>"),
        "Infobox": infobox_page,
        "Out of date": starter_file("Out of date", "<table><tr><td>[b]This article may be out of date.[/b]"
                                    "</td></tr></table>"),
    }
    board.php("$dir = $phpbb_root_path . 'store/phpbbmodders_wiki/templates/';"
              "if (!is_dir($dir)) { mkdir($dir, 0777, true); }"
              "foreach (json_decode(" + php_string(json.dumps(files)) + ", true) as $name => $text) {"
              " file_put_contents($dir . $name . '.wiki', $text); }",
              check=True)
    board.purge_cache()

    shoot.shot("wiki-overview", "app.php/wiki/?sid={sid}", admin=True, full_page=True)
    shoot.shot("wiki-article-view", "app.php/wiki/Getting%20Started?sid={sid}", admin=True)
    shoot.shot("wiki-edit", "app.php/wiki/Getting%20Started?action=edit&sid={sid}", admin=True, full_page=True)
    # The first list on the overview is the pending one, shown to moderators.
    shoot.shot("wiki-pending-articles", "app.php/wiki/?sid={sid}", admin=True, selector=".forumbg")
    # From the live version to the pending edit, so the readable view shows what the edit adds
    shoot.shot("wiki-compare", f"app.php/wiki/Formatting%20Guide?action=compare&from={active}&to={pending}"
               "&sid={sid}", admin=True)

    def open_moderator_controls(p) -> None:
        p.click("#quickmod .dropdown-trigger")
        p.wait_for_selector("#quickmod .dropdown:visible")
    shoot.shot("wiki-moderator-controls", "app.php/wiki/Formatting%20Guide?action=versions&sid={sid}",
               admin=True, prepare=open_moderator_controls)

    # Requirements is linked from Installation and links back, so its page
    # shows both a link and the What links here list.
    shoot.shot("wiki-article-links", "app.php/wiki/Installation?sid={sid}", admin=True,
               selector=".panel.bg1")
    shoot.shot("wiki-what-links-here", "app.php/wiki/Requirements?sid={sid}", admin=True)

    # The lists of templates and categories, with their help box and first row open
    def open_help_and_first_row(p) -> None:
        p.evaluate("document.querySelectorAll('details').forEach((d, i) => { d.open = i < 2; })")
    shoot.shot("wiki-templates", "app.php/wiki/templates?sid={sid}", admin=True, full_page=True,
               prepare=open_help_and_first_row)
    shoot.shot("wiki-categories", "app.php/wiki/categories?sid={sid}", admin=True, full_page=True,
               prepare=open_help_and_first_row)
    shoot.shot("wiki-move", "app.php/wiki/Requirements?action=move&sid={sid}", admin=True)

    shoot.shot("wiki-infobox-navbox", "app.php/wiki/Wiki%20extension?sid={sid}", admin=True, full_page=True)
    shoot.shot("wiki-acp-starter-templates",
               "adm/index.php?i=-phpbbmodders-wiki-acp-templates_module&mode=settings&sid={sid}", admin=True,
               full_page=True)

    # Two people editing one article: the notice on the edit form, and with the lock, the page a
    # second editor gets. Only when the extension has the edit lock.
    has_lock = board.php("echo class_exists('\\\\phpbbmodders\\\\wiki\\\\wiki\\\\edit_lock') ? 'yes' : 'no';").strip().endswith("yes")
    if has_lock:
        # Another member, who has the article open; the board is restored from a clean copy each run
        other = int(board.php(
            "$id = user_add(['username' => 'Jane Editor', 'user_password' => '', 'user_email' => 'jane@example.com', "
            "'group_id' => 2, 'user_type' => USER_NORMAL, 'user_lang' => 'en', 'user_regdate' => time()]);"
            "echo $id;", check=True).strip().splitlines()[-1])

        def edit_conflict(mode: str, article: str) -> None:
            """Set the board's mode and record the other member as editing the article since a while ago."""
            board.php("$config->set('wiki_edit_conflict', " + php_string(mode) + ");"
                      "$db->sql_query('DELETE FROM ' . $table_prefix . 'wiki_edit_lock');"
                      "$db->sql_query('INSERT INTO ' . $table_prefix . 'wiki_edit_lock ' . $db->sql_build_array('INSERT', "
                      "['article_url' => " + php_string(article) + ", 'user_id' => " + str(other) + ", "
                      "'lock_time' => time() - 720, 'lock_expires' => time() + 1080, 'blocked_notified' => 1]));",
                      check=True)
            board.purge_cache()

        edit_conflict("notice", "Getting Started")
        shoot.shot("wiki-edit-notice", "app.php/wiki/Getting%20Started?action=edit&sid={sid}", admin=True,
                   selector="#postingbox")
        edit_conflict("lock", "Installation")
        shoot.shot("wiki-edit-locked", "app.php/wiki/Installation?action=edit&sid={sid}", admin=True)
        shoot.shot("wiki-acp-settings", "adm/index.php?i=-phpbbmodders-wiki-acp-main_module&mode=settings&sid={sid}",
                   admin=True, full_page=True)
        # Back to the default, with nobody editing, for the shots that follow
        board.php("$config->set('wiki_edit_conflict', 'notice');"
                  "$db->sql_query('DELETE FROM ' . $table_prefix . 'wiki_edit_lock');", check=True)

    # Per-article edit protection: the Protect article page with a group ticked, and the note on a
    # protected article. Only when the extension has protection.
    has_protection = board.php("echo class_exists('\\\\phpbbmodders\\\\wiki\\\\wiki\\\\protection') ? 'yes' : 'no';").strip().endswith("yes")
    if has_protection:
        # Configuration, protected for the global moderators
        board.php("$result = $db->sql_query('SELECT group_id FROM ' . GROUPS_TABLE . \" WHERE group_name = 'GLOBAL_MODERATORS'\");"
                  "$group_id = (int) $db->sql_fetchfield('group_id');"
                  "$db->sql_freeresult($result);"
                  "$db->sql_query('INSERT INTO ' . $table_prefix . 'wiki_article_protect ' . $db->sql_build_array('INSERT', "
                  "['article_url' => 'Configuration', 'group_id' => $group_id]));",
                  check=True)
        board.purge_cache()
        shoot.shot("wiki-protect", "app.php/wiki/Configuration?action=protect&sid={sid}", admin=True)
        shoot.shot("wiki-protected-article", "app.php/wiki/Configuration?sid={sid}", admin=True)
        # The lock beside a protected article's title in a list
        shoot.shot("wiki-protected-list", "app.php/wiki/recent-changes?sid={sid}", admin=True, selector=".forumbg")

    # A Start template that asks for its values before it fills a new article
    add_article("Template:Start Release",
                "<noinclude>A release note: asks for the version, the date and who made it.</noinclude>"
                "== Release {{{version|1.0}}} ==\nReleased on {{{date}}} by {{{1}}}.",
                "A release note", days_ago=4)
    board.purge_cache()
    shoot.shot("wiki-start-values", "app.php/wiki/Release%202.0?action=edit&start_from=Start%20Release&sid={sid}",
               admin=True, selector="#page-body")

    # The admin's own BBCodes, one of them made wiki-only. Only when the extension has the setting.
    has_wiki_only = board.php("echo class_exists('\\\\phpbbmodders\\\\wiki\\\\wiki\\\\wiki_only_bbcodes') ? 'yes' : 'no';").strip().endswith("yes")
    if has_wiki_only:
        board.php("$ids = [];"
                  "foreach ([['note=', '[note={SIMPLETEXT}]{TEXT}[/note]', '<div class=\"note\"><b>{SIMPLETEXT}</b> {TEXT}</div>', 'Note box: [note=Title]text[/note]'],"
                  " ['youtube', '[youtube]{URL}[/youtube]', '<a href=\"{URL}\">{URL}</a>', 'YouTube video: [youtube]link[/youtube]']] as $i => $b) {"
                  "  $ids[] = NUM_CORE_BBCODES + $i + 1;"
                  "  $db->sql_query('INSERT INTO ' . BBCODES_TABLE . ' ' . $db->sql_build_array('INSERT', ["
                  "    'bbcode_id' => NUM_CORE_BBCODES + $i + 1, 'bbcode_tag' => $b[0], 'bbcode_match' => $b[1], 'bbcode_tpl' => $b[2],"
                  "    'bbcode_helpline' => $b[3], 'display_on_posting' => 1, 'first_pass_match' => '', 'first_pass_replace' => '',"
                  "    'second_pass_match' => '', 'second_pass_replace' => '']));"
                  "}"
                  "$config->set('wiki_only_bbcodes', (string) $ids[0]);", check=True)
        board.purge_cache()
        shoot.shot("wiki-acp-bbcodes", "adm/index.php?i=-phpbbmodders-wiki-acp-bbcodes_module&mode=settings&sid={sid}",
                   admin=True, full_page=True)

    def highlight_button(p) -> None:
        p.add_style_tag(content=".bbcode-wikilink { outline: 3px solid #d31141; outline-offset: 2px; }")
    shoot.shot("wiki-link-button", "app.php/wiki/Requirements?action=edit&sid={sid}", admin=True,
               selector="#format-buttons", prepare=highlight_button)


# The Knowledge Base's example content, as PHP run inside the board. It makes categories
# and articles with the extension's own text formatter, indexes the live ones for search,
# gives the board two forums for the article announcements and the moderators' changelog,
# and posts three forum questions that the articles answer, so a board search for
# "backup" shows the Knowledge Base block beside forum posts.
KB_SEED_PHP = r"""
$data = json_decode(<<<'JSON'
@DATA@
JSON, true);
$language = $phpbb_container->get('language');
$language->add_lang(['acp/common', 'acp/forums']);
require_once $phpbb_root_path . 'includes/functions_acp.php';
require_once $phpbb_root_path . 'includes/functions_admin.php';
require_once $phpbb_root_path . 'includes/acp/acp_forums.php';
require_once $phpbb_root_path . 'includes/functions_posting.php';
require_once $phpbb_root_path . 'includes/functions_display.php';

$admin = $db->sql_fetchrow($db->sql_query("SELECT * FROM " . USERS_TABLE . " WHERE username = 'admin'"));
$user->data = array_merge($user->data, $admin, ['user_ip' => '127.0.0.1', 'is_registered' => true, 'is_bot' => false]);
$user->ip = '127.0.0.1';
$auth->acl($user->data);

$members = [$admin];
$result = $db->sql_query('SELECT * FROM ' . USERS_TABLE . ' WHERE user_type = 0 ORDER BY user_id LIMIT 2');
while ($row = $db->sql_fetchrow($result)) { $members[] = $row; }
$db->sql_freeresult($result);

// -- forums: announcement, changelog and the one the questions go in
function seed_forum($name, $desc) {
    global $phpbb_root_path;
    $acp_forums = new acp_forums();
    $d = ['parent_id' => 0, 'forum_type' => FORUM_POST, 'type_action' => '', 'forum_status' => ITEM_UNLOCKED,
        'forum_parents' => '', 'forum_name' => $name, 'forum_link' => '', 'forum_link_track' => false,
        'forum_desc' => $desc, 'forum_desc_uid' => '', 'forum_desc_options' => 7, 'forum_desc_bitfield' => '',
        'forum_rules' => '', 'forum_rules_uid' => '', 'forum_rules_options' => 7, 'forum_rules_bitfield' => '',
        'forum_rules_link' => '', 'forum_image' => '', 'forum_style' => 0, 'display_subforum_list' => true,
        'display_subforum_limit' => false, 'display_on_index' => true, 'forum_topics_per_page' => 0,
        'enable_indexing' => true, 'enable_icons' => true, 'enable_prune' => false, 'enable_post_review' => true,
        'enable_quick_reply' => false, 'enable_shadow_prune' => false, 'prune_days' => 7, 'prune_viewed' => 7,
        'prune_freq' => 1, 'prune_old_polls' => false, 'prune_announce' => false, 'prune_sticky' => false,
        'prune_shadow_days' => 7, 'prune_shadow_freq' => 1, 'forum_password' => '', 'forum_password_confirm' => '',
        'forum_password_unset' => false, 'forum_options' => 0, 'show_active' => true];
    $errors = $acp_forums->update_forum_data($d);
    if ($errors) { throw new RuntimeException('forum ' . $name . ': ' . implode(', ', $errors)); }
    // the permissions of the board's first forum, as the ACP's copy option gives
    copy_forum_permissions(2, $d['forum_id'], false);
    return (int) $d['forum_id'];
}
$announce_forum = seed_forum('Knowledge Base Comments', 'Discussion of the Knowledge Base articles.');
$changelog_forum = seed_forum('Knowledge Base Changelog', 'Moderators only: what changed in each article.');
$help_forum = seed_forum('Help and Questions', 'Ask the community for help.');
$groups = [];
$result = $db->sql_query("SELECT group_id, group_name FROM " . GROUPS_TABLE . " WHERE group_name IN ('ADMINISTRATORS', 'GLOBAL_MODERATORS')");
while ($row = $db->sql_fetchrow($result)) { $groups[$row['group_name']] = (int) $row['group_id']; }
$db->sql_freeresult($result);
$phpbb_container->get('config_text')->set('kb_team_groups', json_encode(array_values($groups)));
$config->set('kb_contact_group', isset($groups['GLOBAL_MODERATORS']) ? $groups['GLOBAL_MODERATORS'] : 0);
$config->set('kb_forum_id', $announce_forum);
$config->set('kb_anounce', 1);
$config->set('kb_changelog_forum_id', $changelog_forum);

// -- categories (nested set: left and right numbers run through the tree)
$categories_table = $phpbb_container->getParameter('tables.categories_table');
$articles_table = $phpbb_container->getParameter('tables.articles_table');
$ids = [];
$next = 1;
$walk = function (array $nodes, $parent) use (&$walk, &$next, &$ids, $db, $categories_table) {
    foreach ($nodes as $node) {
        $left = $next++;
        $db->sql_query('INSERT INTO ' . $categories_table . ' ' . $db->sql_build_array('INSERT', [
            'parent_id' => $parent, 'left_id' => $left, 'right_id' => 0, 'category_parents' => '',
            'category_name' => $node['name'], 'category_details' => $node['details']]));
        $id = (int) $db->sql_nextid();
        $ids[$node['key']] = $id;
        $walk(isset($node['children']) ? $node['children'] : [], $id);
        $db->sql_query('UPDATE ' . $categories_table . ' SET right_id = ' . $next++ . ' WHERE category_id = ' . $id);
    }
};
$walk($data['categories'], 0);

// -- articles
$kb = $phpbb_container->get('phpbbmodders.knowledgebase.inc');
$text = $phpbb_container->get('phpbbmodders.knowledgebase.article_text');
$search = $phpbb_container->get('phpbbmodders.knowledgebase.search.backend_factory')->get_active();
$now = time();
$article_ids = [];
foreach ($data['articles'] as $a) {
    $body = $a['text'];
    $uid = $bitfield = $options = '';
    $text->store($body, !empty($a['markdown']), $uid, $bitfield, $options);
    $author = $members[min($a['author'], count($members) - 1)];
    $written = $now - (int) ($a['days'] * 86400);
    $edited = empty($a['edited_days']) ? $written : $now - (int) ($a['edited_days'] * 86400);
    $db->sql_query('INSERT INTO ' . $articles_table . ' ' . $db->sql_build_array('INSERT', [
        'article_category_id' => $ids[$a['category']], 'approved' => $a['approved'], 'status' => $a['status'],
        'article_title' => $a['title'], 'article_description' => $a['description'], 'article_body' => $body,
        'bbcode_uid' => $uid, 'article_markdown' => empty($a['markdown']) ? 0 : 1,
        'author_id' => (int) $author['user_id'], 'author' => $author['username'], 'author_type' => 0,
        'article_date' => $written, 'edit_date' => $edited, 'views' => $a['views']]));
    $id = (int) $db->sql_nextid();
    $article_ids[$a['key']] = $id;
    if ($a['approved'] && $a['status'] == 2) {
        $plain = $a['text'];
        $title = $a['title'];
        $description = $a['description'];
        $search->index('add', $id, $plain, $title, $description, (int) $author['user_id']);
    }
    if (!empty($a['revision'])) {
        $kb->save_article_revision($id, ['article_title' => $a['title'], 'article_description' => $a['description'],
            'article_body' => $a['revision'], 'bbcode_uid' => '', 'bbcode_bitfield' => ''], (int) $author['user_id']);
    }
}
foreach ($ids as $key => $category_id) {
    $db->sql_query('UPDATE ' . $categories_table . ' SET number_articles = (SELECT COUNT(*) FROM ' . $articles_table
        . ' WHERE article_category_id = ' . $category_id . ' AND approved = 1 AND status = 2) WHERE category_id = ' . $category_id);
}

// -- forum questions the articles answer
function seed_post($poster, $forum, $subject, $message, $topic = 0) {
    global $db, $user, $auth;
    $keep = $user->data;
    $user->data = array_merge($keep, $poster, ['user_ip' => '127.0.0.1', 'is_registered' => true, 'is_bot' => false]);
    $auth->acl($user->data);
    $uid = $bitfield = '';
    $options = 0;
    generate_text_for_storage($message, $uid, $bitfield, $options, true, true, true);
    $d = ['forum_id' => $forum, 'topic_id' => $topic, 'topic_title' => $subject, 'icon_id' => 0,
        'poster_id' => (int) $poster['user_id'], 'enable_bbcode' => true, 'enable_smilies' => true, 'enable_urls' => true,
        'enable_sig' => false, 'message' => $message, 'message_md5' => md5($message), 'bbcode_uid' => $uid,
        'bbcode_bitfield' => $bitfield, 'enable_indexing' => true, 'notify' => false, 'notify_set' => false,
        'post_edit_locked' => false, 'topic_time_limit' => 0, 'topic_status' => ITEM_UNLOCKED, 'forum_name' => '',
        'force_approved_state' => ITEM_APPROVED];
    $poll = [];
    submit_post($topic ? 'reply' : 'post', $subject, '', POST_NORMAL, $poll, $d);
    $user->data = $keep;
    $auth->acl($user->data);
    return (int) $d['topic_id'];
}
foreach ($data['questions'] as $i => $q) {
    $asker = $members[1 + ($i % max(1, count($members) - 1))] ?? $admin;
    $topic = seed_post($asker, $help_forum, $q['title'], $q['question']);
    seed_post($admin, $help_forum, 'Re: ' . $q['title'], $q['answer'], $topic);
}
$config->set('kb_articles_per_page', 10);
echo "\nKB-CATEGORIES:" . json_encode($ids) . "\n";
"""

KB_CATEGORIES = [
    {"key": "start", "name": "Getting Started", "details": "First steps with the board.", "children": [
        {"key": "install", "name": "Installation", "details": "Install the board on your own server."},
        {"key": "first", "name": "First Steps", "details": "Accounts, forums and your first post."}]},
    {"key": "ops", "name": "Board Operations", "details": "Run, protect and recover a live community.", "children": [
        {"key": "backups", "name": "Backups and Recovery", "details": "Backup plans and restoration drills."},
        {"key": "security", "name": "Security", "details": "Permissions, updates and access control."}]},
    {"key": "ref", "name": "Reference", "details": "Commands, settings and terms."},
]

LIVE, PENDING, DRAFT = (2, 1), (0, 0), (4, 1)
# status values: 0 new, 2 active, 4 draft (see inc/functions_kb.php); each article is (status, approved)


def kb_article(key: str, category: str, title: str, description: str, text: str, days: float, views: int = 0,
               author: int = 0, markdown: bool = False, state: tuple = LIVE, edited_days: float = 0,
               revision: str = "") -> dict:
    """One example article for KB_SEED_PHP."""
    return {"key": key, "category": category, "title": title, "description": description, "text": text,
            "days": days, "views": views, "author": author, "markdown": markdown, "status": state[0],
            "approved": state[1], "edited_days": edited_days, "revision": revision}


KB_ARTICLES = [
    kb_article("backup", "backups", "Backing Up Your Board",
               "What to copy before an upgrade, and how to restore it.",
               "# Backing up your board\n\nA backup has two parts: the database and the files.\n\n## What to copy\n\n"
               "1. Export the **database** with your host's tools or `mysqldump`.\n"
               "2. Copy the `files/`, `store/` and `images/` folders and `config.php`.\n"
               "3. Keep the copy somewhere other than the server.\n\n## Restoring\n\nImport the database, put the "
               "folders back and purge the cache.\n\n```\nphp bin/phpbbcli.php cache:purge\n```\n\n"
               "See [Restore drills](#restoring) before you need it.", 9, 412, markdown=True, edited_days=1),
    kb_article("restore", "backups", "Restore Drills",
               "Practise a restore before an incident.",
               "[b]Restore drills[/b]\n\nA backup you have never restored is a guess.\n[list][*]Restore into a spare "
               "database.[*]Check that you can log in.[*]Note how long it took.[/list]", 14, 188, author=1),
    kb_article("opsoverview", "ops", "Running a Board Day to Day",
               "The routine jobs that keep a community healthy.",
               "Check the reports queue, read the error log and make sure last night's backup finished.",
               4, 76, author=1),
    kb_article("backupplan", "backups", "Choosing a Backup Schedule",
               "How often to take a backup, and how many to keep.",
               "Take a database backup every day and a file backup every week. Keep the last seven daily "
               "backups and the last four weekly ones.", 20, 97, author=2),
    kb_article("install", "install", "Installing the Board",
               "Step-by-step installation notes.",
               "# Installing the board\n\nUpload the files, open `install/` in your browser and follow the steps.\n\n"
               "Before you start, make a backup of anything already on the server.", 25, 640, markdown=True),
    kb_article("requirements", "install", "Server Requirements",
               "What your server needs.", "You need PHP 8.2 or later, a database and write access to a few folders.",
               27, 233, author=1),
    kb_article("welcome", "start", "Welcome to the Knowledge Base",
               "Start here.", "[b]Welcome![/b] These articles answer the questions members ask most.",
               30, 891),
    kb_article("accounts", "first", "Creating Your Account",
               "Register, confirm and set up your profile.",
               "Register from the link at the top of the board, confirm your e-mail address and fill in your profile.",
               28, 120, author=2),
    kb_article("permissions", "security", "Understanding Permissions",
               "Who can do what, and how to change it.",
               "# Permissions\n\nPermissions belong to groups. Give a group a right, and every member of it has "
               "that right.\n\nAlways **test** a change as an ordinary member.", 16, 301, markdown=True, author=1,
               edited_days=2),
    kb_article("updates", "security", "Keeping the Board Up to Date",
               "Why and how to upgrade.", "Install every security release as soon as it is out, and back up first.",
               11, 150),
    kb_article("commands", "ref", "Command Reference",
               "Commands for maintaining a board.",
               "[code]php bin/phpbbcli.php cache:purge\nphp bin/phpbbcli.php extension:enable vendor/name[/code]",
               33, 175, author=2),
    # waiting for review
    kb_article("pending1", "security", "Hardening Your Server",
               "Settings that make an attack harder.", "Draft text about server hardening, waiting for review.",
               0.5, 0, author=1, state=PENDING),
    kb_article("pending2", "security", "Glossary",
               "Short definitions of common terms.", "Short definitions of terms used on the board.", 0.2, 0,
               author=2, state=PENDING),
    # a live article with a revision waiting for review
    kb_article("withrevision", "first", "Writing Your First Post",
               "Start a topic and reply to one.", "Open a forum, choose New Topic, write your post and submit it.",
               6, 210, author=1,
               revision="Open a forum, choose New Topic, write your post and submit it. Use Preview first."),
    # the admin's own draft
    kb_article("draft", "install", "Moving to a New Server",
               "Notes for a later article.", "Notes about moving a board: copy the files, then the database.",
               0.1, 0, state=DRAFT),
]

KB_QUESTIONS = [
    {"title": "How do I back up my board before an upgrade?",
     "question": "I am about to upgrade and want a backup first. Is there a guide to backing up the database and "
                 "the files, and to restoring it afterwards?",
     "answer": "Yes, the Knowledge Base has an article on backing up your board and one on restore drills."},
    {"title": "Where is the installation guide?",
     "question": "I just downloaded the board and want to install it on my own server. Is there a guide for the "
                 "installation?",
     "answer": "See Installing the Board in the Knowledge Base."},
    {"title": "How often should I take a backup?",
     "question": "Is a weekly backup enough for a small board, or should I back up every day?",
     "answer": "Daily for the database is safer. The Knowledge Base explains a backup schedule."},
]


def shots_knowledgebase(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/knowledgebase: articles, the approval queue, search, the ACP and the recent changes page.

    Makes its own categories, articles and forum questions, so --seed is only needed for
    more forum content. The ACP pages are taken in full, so a new setting is not cut off.
    """
    data = json.dumps({"categories": KB_CATEGORIES, "articles": KB_ARTICLES, "questions": KB_QUESTIONS})
    out = board.php(KB_SEED_PHP.replace("@DATA@", data), check=True)
    marker = [line for line in out.splitlines() if line.startswith("KB-CATEGORIES:")]
    if not marker:
        shown = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", out)).strip()
        raise RuntimeError(f"the Knowledge Base seed gave no category ids: {shown[:600]}")
    category = json.loads(marker[-1].split(":", 1)[1])
    article = board.sql("SELECT article_id FROM phpbb_kb_articles WHERE article_title = 'Backing Up Your Board'")[0][0]
    board.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'kb_hierarchical_index'")
    board.purge_cache()

    kb = "app.php/knowledgebase"
    acp = "adm/index.php?i=-phpbbmodders-knowledgebase-acp-{}_module&mode={}&sid={{sid}}"
    shoot.shot("01-flat-index", f"{kb}?sid={{sid}}", admin=True)
    shoot.shot("02-article", f"{kb}/article?k={article}&sid={{sid}}", admin=True)
    shoot.shot("03-manage-your-articles", f"{kb}/my_articles?sid={{sid}}", admin=True)
    shoot.shot("04-add-article", f"{kb}/posting?id={category['install']}&sid={{sid}}", admin=True)
    shoot.shot("05-approval-queue", f"{kb}/queue?sid={{sid}}", admin=True)
    shoot.shot("06-search-results", f"{kb}/search?keywords=backup&sid={{sid}}", admin=True)
    shoot.shot("07-acp-configuration", acp.format("config", "settings"), admin=True, full_page=True)
    shoot.shot("09-board-search-kb-block", "search.php?keywords=backup&sid={sid}", admin=True)
    shoot.shot("10-advanced-search-option", "search.php?sid={sid}", admin=True, selector=".panel:has(#kb_also)")
    shoot.shot("11-acp-search-settings", acp.format("search", "index"), admin=True, full_page=True)
    shoot.shot("12-recent-changes", f"{kb}/recent_changes?sid={{sid}}", admin=True)

    # The category tree is the one page that needs hierarchical browsing switched on.
    board.sql("UPDATE phpbb_config SET config_value = '1' WHERE config_name = 'kb_hierarchical_index'")
    board.purge_cache()
    shoot.shot("08-category-tree", f"{kb}?sid={{sid}}", admin=True)


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
    # Example honeypot trips (sfscompanion 1.1+), newest first.
    now = int(time.time())
    trips = (
        (now - 300, "register", "field", 4, 1, "casino_deals", "deals@example.com", "203.0.113.9", "", "", 1, 1),
        (now - 1800, "register", "time", 1, 1, "seo_expert_99", "seo@example.org", "198.51.100.41", "", "", 0, 0),
        (now - 5400, "posting", "field", 12, 3, "free_followers", "followers@example.com", "192.0.2.77",
         "Buy followers today", "Get 10,000 followers in one day. Visit our site for cheap deals...", 0, 0),
        (now - 9000, "pm", "time", 2, 3, "free_followers", "followers@example.com", "192.0.2.77",
         "Hello friend", "I have a great business offer for you...", 0, 0),
    )
    if board.sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'phpbb_sfsc_honeypot_trips'"):
        for trip in trips:
            board.sql("INSERT INTO phpbb_sfsc_honeypot_trips (trip_time, trip_form, trip_reason, submit_seconds, "
                      "user_id, username, user_email, user_ip, user_agent, subject, excerpt, sfs_reported, ip_banned) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Mozilla/5.0 (X11; Linux x86_64)', ?, ?, ?, ?)",
                      trip)
    page = "adm/index.php?i=-phpbbmodders-sfscompanion-acp-main_module&sid={sid}&mode="
    shoot.shot("sfscompanion-acp-blocks", page + "blocks", admin=True)
    shoot.shot("sfscompanion-acp-errors", page + "errors", admin=True)
    shoot.shot("sfscompanion-acp-settings", page + "settings", admin=True)
    if board.sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'phpbb_sfsc_honeypot_trips'"):
        shoot.shot("sfscompanion-acp-honeypot", page + "honeypot", admin=True)
        shoot.shot("sfscompanion-acp-honeypot-trips", page + "honeypot_trips", admin=True)
    member = board.sql("SELECT user_id FROM phpbb_users WHERE user_type = 0 AND user_id <> 2 "
                       "ORDER BY user_posts DESC, user_id LIMIT 1")
    member_id = member[0][0] if member else 2
    def highlight_link(p) -> None:
        p.add_style_tag(content="a[href*='sfs-companion/finder'] { background: #ffd54f; border-radius: 3px; padding: 2px 3px; }")
    shoot.shot("sfscompanion-profile-link", f"memberlist.php?mode=viewprofile&u={member_id}&sid={{sid}}",
               admin=True, selector=".panel:has(a[href*='sfs-companion/finder'])", prepare=highlight_link)


CONTACTADMIN_CONFIG = {
    # Private-message delivery to the founder, no captcha: matches the earlier screenshots
    "contactadmin_method": "2",
    "contactadmin_who": "2",
    "contactadmin_confirm": "0",
    "contactadmin_gdpr": "0",
    "contactadmin_username_chk": "0",
    "contactadmin_bot_poster": "1",
    "contactadmin_forum": "2",
    # Example addresses have no mail server
    "email_check_mx": "0",
}
CONTACTADMIN_REASONS = "A question about the board\nA problem with my account\nSomething else"


def set_config(board: Board, settings: dict) -> None:
    """Set phpBB config values and purge the cache so the board reads them."""
    for name, value in settings.items():
        board.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, name))
    board.purge_cache()


def shots_contactadmin(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/contactadmin: the contact form and its ACP settings.

    contactadmin-acp-settings, -guest-form and -member-basic are the
    documentation gallery (docs/images/screenshots/). contactadmin-acp-delivery,
    -guest-checks and -guest-topic show the 10/18/2026 fixes for the dated
    update gallery: the delivery settings with the board's email turned off,
    the guest form with the username rules and privacy policy box, and a
    guest's message posted by the contact bot, labelled "Guest User".
    """
    set_config(board, CONTACTADMIN_CONFIG)
    acp = "adm/index.php?i=-phpbbmodders-contactadmin-acp-contactadmin_module&mode=configuration&sid={sid}"
    shoot.shot("contactadmin-acp-settings", acp, admin=True, full_page=True)
    shoot.shot("contactadmin-guest-form", "app.php/contactadmin")
    shoot.shot("contactadmin-member-basic", "app.php/contactadmin?sid={sid}", admin=True)

    shoot.shot("contactadmin-acp-delivery", acp, admin=True,
               selector="fieldset:has(input[name='contact_method'])")
    board.sql("UPDATE phpbb_config_text SET config_value = ? WHERE config_name = 'contactadmin_reasons'",
              (CONTACTADMIN_REASONS,))
    set_config(board, {"contactadmin_gdpr": "1", "contactadmin_username_chk": "1"})
    shoot.shot("contactadmin-guest-checks", "app.php/contactadmin")

    # Send a guest message by forum post, then show the topic the contact bot posted
    set_config(board, {"contactadmin_method": "1", "contactadmin_gdpr": "0", "contactadmin_username_chk": "0"})
    context = shoot.browser.new_context(viewport=DESKTOP, locale="en-GB")
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    try:
        page.goto(f"{board.base}/app.php/contactadmin", wait_until="networkidle")
        page.fill("input[name=username]", "Jane Example")
        page.fill("input[name=email]", "jane@example.com")
        page.fill("input[name=email_confirm]", "jane@example.com")
        page.select_option("select[name=contact_reason]", "A question about the board")
        page.fill("textarea[name=message]", "Hello, how do I change the email address on my account?")
        page.wait_for_timeout(1500)  # phpBB rejects forms submitted too quickly
        page.click("input[name=submit]")
        sent = "sent successfully" in page.inner_text("body")
    finally:
        context.close()
    if not sent:
        shoot.failed.append("contactadmin-guest-topic")
        print("  [FAIL] contactadmin-guest-topic: the contact form did not send")
        return
    topic = board.sql("SELECT MAX(topic_id) FROM phpbb_topics")[0][0]

    def hide_who_is_online(p) -> None:
        p.add_style_tag(content=".stat-block { display: none; }")
    shoot.shot("contactadmin-guest-topic", f"viewtopic.php?t={topic}&sid={{sid}}", admin=True,
               selector="#page-body", prepare=hide_who_is_online)


NEWSPAGE_NEWS = [
    ("Board upgraded to the latest phpBB release",
     "We have moved the board to the latest phpBB release. Pages load faster, and the editor now "
     "remembers your draft if you lose your connection.\n\n[b]What you need to do:[/b] nothing. "
     "If something looks wrong, clear your browser cache and let us know in the Support forum."),
    ("New members' area is open",
     "The members' area now has its own forum for introductions and project showcases. "
     "Say hello, tell us what you are working on, and share screenshots of your setup."),
    ("Scheduled maintenance this weekend",
     "The board will be read-only for about an hour on Saturday morning while we move to a new server. "
     "Posts made just before the move are kept."),
]

NEWSPAGE_SEED = r"""
require_once $phpbb_root_path . 'includes/functions_posting.php';
require_once $phpbb_root_path . 'includes/functions_admin.php';
// the install's welcome topic is in the same forum; the news page shows only the example news
delete_topics('forum_id', [2]);
$news = json_decode(%s, true);
$keep = $user->data;
$user->data = array_merge($keep, ['user_id' => 2, 'username' => 'admin', 'user_ip' => '127.0.0.1',
    'is_registered' => true, 'is_bot' => false]);
$auth->acl($user->data);
$ids = [];
foreach ($news as $i => $item) {
    list($subject, $message) = $item;
    $uid = $bitfield = '';
    $options = 0;
    generate_text_for_storage($message, $uid, $bitfield, $options, true, true, true);
    $d = ['forum_id' => 2, 'topic_id' => 0, 'topic_title' => $subject, 'icon_id' => 0, 'poster_id' => 2,
        'enable_bbcode' => true, 'enable_smilies' => true, 'enable_urls' => true, 'enable_sig' => false,
        'message' => $message, 'message_md5' => md5($message), 'bbcode_uid' => $uid, 'bbcode_bitfield' => $bitfield,
        'enable_indexing' => true, 'notify' => false, 'notify_set' => false, 'post_edit_locked' => false,
        'topic_time_limit' => 0, 'topic_status' => ITEM_UNLOCKED, 'forum_name' => '',
        'force_approved_state' => ITEM_APPROVED, 'post_time' => time() - (count($news) - $i) * 86400];
    $poll = [];
    submit_post('post', $subject, '', POST_NORMAL, $poll, $d);
    $ids[] = (int) $d['topic_id'];
}
$user->data = $keep;
echo "\nNEWS-TOPICS:" . json_encode($ids) . "\n";
"""


def shots_newspage(board: Board, shoot: Shooter, args: argparse.Namespace) -> None:
    """phpbbmodders/newspage: the news page, one news item and the ACP settings.

    Replaces the install's welcome topic in the board's first forum with three
    news topics and makes it the news forum. Poster information and the post
    buttons are turned off, so the news text stands out; the other settings
    stay at their defaults. Three more shots of the news page show it with
    poster information and post buttons on (as the admin, who sees every
    button), with the news shortened to 120 characters, and without the
    forum and date filters.
    """
    out = board.php(NEWSPAGE_SEED % php_string(json.dumps(NEWSPAGE_NEWS)), check=True)
    topics = json.loads(out.split("NEWS-TOPICS:")[1].split()[0])
    set_config(board, {"news_forums": "2", "news_user_info": "0", "news_post_buttons": "0"})

    shoot.shot("newspage-news", "app.php/news")
    shoot.shot("newspage-single", f"app.php/news/{topics[0]}")
    shoot.shot("newspage-acp-settings", "adm/index.php?i=-phpbbmodders-newspage-acp-main_module&mode=settings&sid={sid}",
               admin=True, full_page=True)
    shoot.shot("newspage-news-phone", "app.php/news", phone=True)

    # The same page with other settings, one change at a time
    set_config(board, {"news_user_info": "1", "news_post_buttons": "1"})
    shoot.shot("newspage-news-full", "app.php/news?sid={sid}", admin=True)
    set_config(board, {"news_user_info": "0", "news_post_buttons": "0", "news_char_limit": "120"})
    shoot.shot("newspage-news-shortened", "app.php/news")
    set_config(board, {"news_char_limit": "500", "news_cat_show": "0", "news_archive_show": "0"})
    shoot.shot("newspage-news-no-filters", "app.php/news")


SHOTS = {
    "phpbbmodders/contactadmin": shots_contactadmin,
    "phpbbmodders/documentation": shots_documentation,
    "phpbbmodders/knowledgebase": shots_knowledgebase,
    "phpbbmodders/newspage": shots_newspage,
    "phpbbmodders/sfscompanion": shots_sfscompanion,
    "phpbbmodders/stopforumspam": shots_stopforumspam,
    "phpbbmodders/wiki": shots_wiki,
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
