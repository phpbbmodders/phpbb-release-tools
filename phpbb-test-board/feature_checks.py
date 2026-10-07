#!/usr/bin/env python3
"""
Exercise the main feature of phpbbmodders extensions on a local test board.

For each extension checkout given, installs it into a clean copy of the board
(created by setup-board.sh), after the --with extensions it requires, serves the board, runs that extension's feature
check over HTTP, and restores the board. Checks exist for the extensions in
CHECKS below, keyed by composer name; add one there for a new extension.
Test users get random throwaway passwords that are never printed.

Exit status: 0 if every check passed, 1 if any failed, 2 for bad arguments.
"""
import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

import requests  # noqa: E402

from _board import Board, ext_name, parse_spec, required_packages  # noqa: E402

results: list = []


def ok(cond: object, label: str) -> None:
    """Record and print one check result."""
    results.append((bool(cond), label))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


def form_fields(html: str, form_id: str = "") -> tuple:
    """Return (action, hidden fields) of a form, by id or the first POST form."""
    pattern = r'(?s)<form[^>]*id="%s"[^>]*>.*?</form>' % form_id if form_id else \
        r'(?s)<form[^>]*method="post"[^>]*>.*?</form>'
    m = re.search(pattern, html)
    block = m.group(0) if m else html
    action = re.search(r'<form[^>]*action="([^"]*)"', block)
    fields = dict(re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', block))
    return (action.group(1).replace("&amp;", "&") if action else ""), fields


def post_back(session: requests.Session, base_url: str, page_url: str, action: str, data: dict) -> str:
    """Submit a form the way a browser would (action="" posts to the same page)."""
    time.sleep(1.1)  # phpBB's minimum time before a form may be submitted
    url = f"{base_url}/" + action.lstrip("./") if action else page_url
    return session.post(url, data=data).text


# -- checks, one per extension ---------------------------------------------

def check_useridviewtopic(b: Board) -> None:
    body = requests.get(f"{b.base}/viewtopic.php?t=1").text
    ok(re.search(r"User ID:\s*</strong>\s*2\b", body), "on by default: poster's user ID shown under their rank")
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'load_userid_viewtopic'")
    b.purge_cache()
    ok("User ID" not in requests.get(f"{b.base}/viewtopic.php?t=1").text, "setting off: no user ID shown")


def check_banlist(b: Board) -> None:
    b.add_user("bannedprobe", secrets.token_urlsafe(12))
    b.php("user_ban('user', 'bannedprobe', 0, 0, 0, 'probe reason', 'probe reason');")
    admin = requests.Session()
    b.login(admin)
    body = admin.get(f"{b.base}/app.php/banlist").text
    ok("bannedprobe" in body, "banned user appears on the ban list")
    ok("probe reason" in body, "ban reason shown")
    ok(requests.get(f"{b.base}/app.php/banlist").status_code == 403, "guest without permission gets 403")


def check_bannerrotator(b: Board) -> None:
    img_dir = b.root / "images/bannerrotator"
    img_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy(b.root / "images/smilies/icon_e_smile.gif", img_dir / "probe.gif")
        b.sql("INSERT INTO phpbb_bannerrotator_banners (banner_title, image, url, weight, target_blank, "
              "rel_nofollow, active, date_start, date_end) VALUES ('Probe banner', 'probe.gif', "
              "'https://example.com/', 1, 1, 0, 1, 0, 0)")
        for key in ("phpbbmodders_bannerrotator_enabled", "phpbbmodders_bannerrotator_show_index"):
            b.sql("UPDATE phpbb_config SET config_value = '1' WHERE config_name = ?", (key,))
        b.purge_cache()
        body = requests.get(f"{b.base}/index.php").text
        m = re.search(r'<img src="([^"]*bannerrotator/probe\.gif)" alt="Probe banner"', body)
        ok(m, "active banner shown on the board index")
        ok('href="https://example.com/"' in body, "banner links to its target URL")
        if m:
            ok(requests.get(f"{b.base}/{m.group(1).lstrip('./')}").status_code == 200, "banner image URL loads")
        b.sql("UPDATE phpbb_bannerrotator_banners SET date_end = 1000")
        b.purge_cache()
        ok("probe.gif" not in requests.get(f"{b.base}/index.php").text, "expired banner not shown")
    finally:
        shutil.rmtree(img_dir, ignore_errors=True)


def check_groupwarn(b: Board) -> None:
    mod_pw = secrets.token_urlsafe(12)
    mod_id = b.add_user("gwmod", mod_pw)
    target_id = b.add_user("gwtarget", secrets.token_urlsafe(12))
    b.php(f"group_user_add(4, [{mod_id}]);")  # Global moderators
    s = requests.Session()
    b.login(s, "gwmod", mod_pw)
    url = f"{b.base}/mcp.php?i=warn&mode=warn_user&u={target_id}"

    def submit_warning() -> str:
        action, fields = form_fields(s.get(url).text, "mcp")
        return post_back(s, b.base, url, action,
                         {**fields, "warning": "probe warning", "action[add_warning]": "Submit"})

    def warnings() -> int:
        return b.sql("SELECT user_warnings FROM phpbb_users WHERE user_id = ?", (target_id,))[0][0]

    body = submit_warning()
    ok("You cannot warn this user" in body and warnings() == 0,
       "moderator can't warn a user whose groups aren't ticked (default)")
    groups = [r[0] for r in b.sql("SELECT group_id FROM phpbb_user_group WHERE user_id = ?", (target_id,))]
    b.sql(f"UPDATE phpbb_groups SET group_warn = 1 WHERE group_id IN ({','.join('?' * len(groups))})", tuple(groups))
    b.purge_cache()
    body = submit_warning()
    ok("You cannot warn this user" not in body and warnings() == 1,
       "moderator can warn once all the user's groups are ticked")


def check_documentation(b: Board) -> None:
    # The default build location moved over time (ext/.../docs-build, then
    # store/phpbbmodders_documentation), so write the probe build wherever
    # the installed version is configured to look.
    rows = b.sql("SELECT config_value FROM phpbb_config "
                 "WHERE config_name = 'phpbbmodders_documentation_docs_path'")
    if not rows:
        ok(False, "docs build path is configured")
        return
    build = b.root / rows[0][0]
    # store/ isn't reset between runs the way ext/ is, so start from an
    # empty build rather than whatever an earlier run left there.
    if build.resolve().is_relative_to(b.root.resolve()):
        shutil.rmtree(build, ignore_errors=True)
    for sub, marker in (("en", "PROBE-DOCS-HOME"), ("en/userguide", "PROBE-DOCS-SECTION")):
        (build / sub).mkdir(parents=True, exist_ok=True)
        (build / sub / "index.html").write_text(
            f'<html><head><title>{marker}</title></head><body>'
            f'<nav class="docs-nav-panel"><a href="/en/userguide/">User guide</a></nav>'
            f'<div class="utility-bar">Home</div><article class="docs-article"><h1>{marker}</h1></article></body></html>')
    g = requests.Session()
    g.get(f"{b.base}/index.php")  # the first request runs any permission sync left pending by the install
    perms = [r[0] for r in b.sql("SELECT auth_option FROM phpbb_acl_options "
                                 "WHERE auth_option LIKE 'u_phpbbmodders_documentation_%'")]
    ok("u_phpbbmodders_documentation_lang_en" in perms and "u_phpbbmodders_documentation_userguide" in perms,
       "language and section permissions exist")
    b.purge_cache()
    r = g.get(f"{b.base}/app.php/documentation")
    ok(r.status_code == 200 and "PROBE-DOCS-HOME" in r.text, f"guest can read the docs home ({r.status_code})")
    r = g.get(f"{b.base}/app.php/documentation/en/userguide")
    ok(r.status_code == 200 and "PROBE-DOCS-SECTION" in r.text, f"guest can read a section page ({r.status_code})")
    # Versions with a server-side search fallback answer a search without
    # JavaScript from <lang>/<section>/search-index.json.
    helper = b.root / "ext/phpbbmodders/documentation/controller/documentation_helper.php"
    if "SEARCH_INDEX_FILE" in helper.read_text(encoding="utf-8"):
        (build / "en/userguide/search-index.json").write_text(json.dumps(
            [{"url": "/en/userguide/", "title": "PROBE-SEARCH-TITLE", "text": "probe searchable words"}]))
        r = g.get(f"{b.base}/app.php/documentation-search/en", params={"q": "searchable words"})
        ok(r.status_code == 200 and "PROBE-SEARCH-TITLE" in r.text and "documentation/en/userguide" in r.text,
           f"search without JavaScript returns server results ({r.status_code})")
        r = g.get(f"{b.base}/app.php/documentation-search/en", params={"q": "nothing matches this"})
        ok(r.status_code == 200 and "PROBE-SEARCH-TITLE" not in r.text and 'role="status"' in r.text,
           f"search without JavaScript reports no results ({r.status_code})")
    # A guest who loses access gets the login form rather than a bare 403.
    b.sql("DELETE FROM phpbb_acl_groups "
          "WHERE group_id = (SELECT group_id FROM phpbb_groups WHERE group_name = 'GUESTS') "
          "AND auth_option_id = (SELECT auth_option_id FROM phpbb_acl_options "
          "WHERE auth_option = 'u_phpbbmodders_documentation_userguide')")
    b.sql("UPDATE phpbb_users SET user_permissions = ''")
    b.purge_cache()
    r = requests.Session().get(f"{b.base}/app.php/documentation/en/userguide")
    ok('name="username"' in r.text and "PROBE-DOCS-SECTION" not in r.text,
       f"guest without access gets the login form ({r.status_code})")


def check_separatebots(b: Board) -> None:
    requests.get(f"{b.base}/index.php",
                 headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"})
    b.purge_cache()
    admin = requests.Session()
    b.login(admin)  # guests don't see the online names list by default
    body = admin.get(f"{b.base}/index.php").text
    ok(re.search(r"<br\s*/?>\s*Bots:\s*.*Google \[Bot\]", body), "a visiting bot is listed on its own 'Bots:' line")


def check_reassignthumbs(b: Board) -> None:
    upload = b.root / "files"
    names = ["probe_image1", "probe_image2", "probe_image3"]
    try:
        for n in names:
            subprocess.run(["convert", "-size", "800x600", "plasma:", "jpg:" + str(upload / n)], check=True)
        rows = [(n, (upload / n).stat().st_size) for n in names] + [("probe_missing", 50000)]  # no file
        for n, size in rows:
            b.sql("INSERT INTO phpbb_attachments (post_msg_id, topic_id, in_message, poster_id, is_orphan, "
                  "physical_filename, real_filename, extension, mimetype, filesize, filetime, thumbnail) "
                  "VALUES (1, 1, 0, 2, 0, ?, 'probe.jpg', 'jpg', 'image/jpeg', ?, 1, 0)", (n, size))
        for key, value in (("img_create_thumbnail", "1"), ("img_min_thumb_filesize", "0"), ("img_max_thumb_width", "400")):
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, key))
        b.purge_cache()
        admin = requests.Session()
        sid = b.login(admin, acp=True)
        mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%reassignthumbs%' "
                    "AND module_mode = 'tools'")[0][0]
        page_url = f"{b.base}/adm/index.php?i={mid}&mode=tools&sid={sid}"
        action, fields = form_fields(admin.get(page_url).text)
        ok("form_token" in fields, "the ACP form carries a form token")
        body = post_back(admin, f"{b.base}/adm", page_url, action, {**fields, "submit": "1", "limit": "2"})
        requests_made = 1
        while requests_made <= 10:
            m = re.search(r'http-equiv="refresh" content="\d+;url=([^"]+)"', body)
            if not m:
                break
            body = admin.get(f"{b.base}/adm/" + m.group(1).replace("&amp;", "&").lstrip("./")).text
            requests_made += 1
        ok("form was invalid" not in body.lower(), f"no 'invalid form' error ({requests_made} requests)")
        ok(requests_made <= 10, "the batches finish instead of looping")
        made = [n for n in names if (upload / f"thumb_{n}").exists()]
        ok(len(made) == 3, f"all 3 images got thumbnails across batches of 2 ({len(made)}/3)")
    finally:
        for n in names:
            for f in (n, f"thumb_{n}"):
                (upload / f).unlink(missing_ok=True)


def check_honeypot(b: Board) -> None:
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'enable_confirm'")
    b.purge_cache()
    g = requests.Session()
    _, fields = form_fields(g.get(f"{b.base}/ucp.php?mode=register").text, "agreement")
    time.sleep(1.1)
    page = g.post(f"{b.base}/ucp.php?mode=register", data={**fields, "agreed": "I agree to these terms"}).text
    action, fields = form_fields(page, "register")
    ok('name="hp_website"' in page, "registration form carries the hidden honeypot fields")
    time.sleep(6)  # stay above the minimum time-to-submit, so only the hidden field trips
    page = g.post(f"{b.base}/" + action.lstrip("./"), data={
        **fields, "username": "botprobe", "email": "botprobe@example.com", "new_password": "Probe-pw-12345",
        "password_confirm": "Probe-pw-12345", "lang": "en", "tz": "UTC",
        "hp_website": "http://spam.example/", "submit": "Submit"}).text
    ok("Your submission could not be processed" in page, "filled-in hidden field: registration rejected")
    ok(not b.sql("SELECT 1 FROM phpbb_users WHERE username_clean = 'botprobe'"), "no account was created")
    ok(b.sql("SELECT 1 FROM phpbb_log WHERE log_operation = 'LOG_HONEYPOT_TRIPPED'"), "the trip was logged")


def check_adduser(b: Board) -> None:
    admin = requests.Session()
    sid = b.login(admin, acp=True)
    mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%adduser%' AND module_mode <> ''")[0][0]
    page_url = f"{b.base}/adm/index.php?i={mid}&sid={sid}"
    action, fields = form_fields(admin.get(page_url).text)
    ok("form_token" in fields, "Add User form carries a form token")
    post_back(admin, f"{b.base}/adm", page_url, action, {
        **fields, "username": "addedprobe", "email": "addedprobe@example.com", "new_password": "Probe-pw-12345",
        "password_confirm": "Probe-pw-12345", "lang": "en", "group": "4", "submit": "Submit"})
    row = b.sql("SELECT user_id, user_email FROM phpbb_users WHERE username_clean = 'addedprobe'")
    ok(row and row[0][1] == "addedprobe@example.com", "the account was created with the given email")
    if row:
        ok(b.sql("SELECT 1 FROM phpbb_user_group WHERE user_id = ? AND group_id = 4", (row[0][0],)),
           "the user was added to the chosen group (Global moderators)")
    ok(b.sql("SELECT 1 FROM phpbb_log WHERE log_operation = 'LOG_USER_ADDED'"), "the new account was logged")


def check_sfscompanion(b: Board) -> None:
    # Log entries in the shape rmcgirr83/stopforumspam writes them, plus one
    # unrelated admin entry that neither log page may show.
    b.php("$phpbb_log->add('user', 2, '203.0.113.9', 'LOG_SFS_MESSAGE', false, "
          "['reportee_id' => 2, 'probe-spammer', '203.0.113.9', 'probe-spammer@example.com']);"
          "$phpbb_log->add('admin', 2, '198.51.100.7', 'LOG_SFS_DOWN', false, ['probe-down@example.com']);"
          "$phpbb_log->add('admin', 2, '198.51.100.8', 'LOG_CONFIG_SETTINGS');")
    admin = requests.Session()
    sid = b.login(admin, acp=True)
    page = f"{b.base}/adm/index.php?i=-phpbbmodders-sfscompanion-acp-main_module&sid={sid}&mode="

    blocks = admin.get(page + "blocks").text
    ok("probe-spammer" in blocks, "Spam blocks page lists the blocked registration")
    ok("was down" not in blocks and "Altered board settings" not in blocks, "Spam blocks page shows nothing else")
    errors = admin.get(page + "errors").text
    ok("was down" in errors, "SFS errors page lists the outage")
    ok("probe-spammer" not in errors and "Altered board settings" not in errors, "SFS errors page shows nothing else")
    ok("probe-spammer" in admin.get(page + "blocks&isearch=203.0.113.*").text
       and "probe-spammer" not in admin.get(page + "blocks&isearch=192.0.2.*").text, "IP search filters the list")

    action, fields = form_fields(blocks, "sfs_log")
    body = post_back(admin, f"{b.base}/adm", page + "blocks", action, {**fields, "delall": "Delete all"})
    action, fields = form_fields(body, "confirm")
    ok("confirm_uid" in fields, "Delete all asks for confirmation")
    post_back(admin, f"{b.base}/adm", page + "blocks", action, {**fields, "delall": "1", "confirm": "Yes"})
    ops = [r[0] for r in b.sql("SELECT log_operation FROM phpbb_log")]
    ok("LOG_SFS_MESSAGE" not in ops and "LOG_SFS_DOWN" in ops and "LOG_CONFIG_SETTINGS" in ops,
       "Delete all removed only the spam block entries")
    ok("LOG_CLEAR_SFS_BLOCKS" in ops, "the deletion was logged")

    settings = page + "settings"
    action, fields = form_fields(admin.get(settings).text, "acp_sfs_settings")
    post_back(admin, f"{b.base}/adm", settings, action, {**fields, "sfsc_expire_days": "7", "submit": "Submit"})
    ok(b.sql("SELECT config_value FROM phpbb_config WHERE config_name = 'sfsc_expire_days'") == [("7",)],
       "settings: log prune interval saved")

    ok("sfs-companion/finder" in admin.get(f"{b.base}/memberlist.php?mode=viewprofile&u=2").text,
       "admin sees the 'Check via StopForumSpam' link on a profile")
    member_pw = secrets.token_urlsafe(12)
    b.add_user("sfsmember", member_pw)
    member = requests.Session()
    b.login(member, "sfsmember", member_pw)
    profile = member.get(f"{b.base}/memberlist.php?mode=viewprofile&u=2").text
    ok("sfsmember" in profile and "sfs-companion/finder" not in profile,
       "a signed-in member without m_chk_sfs doesn't see the link")
    ok("not authorised" in member.get(f"{b.base}/app.php/sfs-companion/finder?u=2").text,
       "a member without m_chk_sfs can't open the lookup page")


CHECKS = {
    "phpbbmodders/useridviewtopic": check_useridviewtopic,
    "phpbbmodders/banlist": check_banlist,
    "phpbbmodders/bannerrotator": check_bannerrotator,
    "phpbbmodders/groupwarn": check_groupwarn,
    "phpbbmodders/documentation": check_documentation,
    "phpbbmodders/separatebots": check_separatebots,
    "phpbbmodders/reassignthumbs": check_reassignthumbs,
    "phpbbmodders/honeypot": check_honeypot,
    "phpbbmodders/adduser": check_adduser,
    "phpbbmodders/sfscompanion": check_sfscompanion,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Exercise phpbbmodders extensions' main features on a local test board.",
                                 epilog="Extensions with checks: " + ", ".join(sorted(CHECKS))
                                        + ". Exit status: 0 all passed, 1 a check failed, 2 bad arguments.")
    ap.add_argument("repos", nargs="+", metavar="REPO[@REF]",
                    help="extension git checkout, optionally with a git ref to install (default ref: HEAD)")
    ap.add_argument("-w", "--with", dest="needed", action="append", default=[], metavar="PATH[@REF]",
                    help="git checkout of an extension that a tested extension requires (default ref: HEAD); "
                         "installed and enabled first, only for the extensions whose composer.json requires it; "
                         "repeat for several, in the order they must be enabled")
    ap.add_argument("-b", "--board-dir",
                    help="board directory created by setup-board.sh (default: $PHPBB_TEST_BOARD)")
    ap.add_argument("-p", "--port", type=int, default=8083,
                    help="port for the temporary web server (default: 8083)")
    args = ap.parse_args()

    try:
        targets = [parse_spec(spec) for spec in args.repos]
        needed = [(ext_name(r, ref), r, ref) for r, ref in (parse_spec(spec) for spec in args.needed)]
    except ValueError as e:
        ap.error(str(e))
    board = Board(args.board_dir, args.port)

    for repo, ref in targets:
        requires = required_packages(repo, ref)
        deps = [(r, dep_ref) for name, r, dep_ref in needed if name in requires]
        for package in requires:
            if package not in {name for name, _, _ in needed}:
                print(f"  [note] {repo.name} requires {package}; if it is a phpBB extension, add --with PATH")
        ext, target, out = board.install_ext(repo, ref, deps)
        print(f"== {ext} ({repo.name} @ {ref})")
        try:
            if ext not in CHECKS:
                print("  [skip] no feature check for this extension")
                continue
            if "Successfully" not in out:
                ok(False, f"enable: {out.strip()[-200:]}")
                continue
            with board.serve():
                CHECKS[ext](board)
            errors = board.php_errors()
            ok(not errors, "no PHP errors logged" + (f": {errors[0][:200]}" if errors else ""))
        finally:
            board.remove_ext(target)

    failed = [label for good, label in results if not good]
    print(f"== {len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
