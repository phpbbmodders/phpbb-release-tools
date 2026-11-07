#!/usr/bin/env python3
"""
Exercise the main feature of phpbbmodders extensions on a local test board.

For each extension checkout given, installs it into a clean copy of the board
(created by setup-board.sh), after the --with extensions it requires, serves the board, runs that extension's feature
check over HTTP, and restores the board. Checks exist for the extensions in
CHECKS below, keyed by composer name; add one there for a new extension.
With --seed-board the same checks run against an installed seed-forum board
instead, on whichever database it uses, followed by a disable/purge/enable
round trip of each extension through phpBB's CLI.
Test users get random throwaway passwords that are never printed.

Exit status: 0 if every check passed, 1 if any failed, 2 for bad arguments.
"""
import argparse
import hashlib
import json
import re
import secrets
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

from _board import Board, SeedBoard, ext_name, parse_spec, required_packages  # noqa: E402

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


def check_wiki(b: Board) -> None:
    # Articles and a post stored through phpBB's own text parser, so the
    # extension's [[ ]] markup is parsed exactly as on a real board.
    def store(text: str) -> str:
        return ("$t = " + json.dumps(text) + "; $uid = $bit = ''; $flags = 0;"
                "generate_text_for_storage($t, $uid, $bit, $flags, true, true, true);")

    articles = (
        ("First article", "Read [[Second article]] and [[Missing page|this one]]."),
        ("Second article", "The second article."),
    )
    for title, text in articles:
        b.php(store(text) + "$db->sql_query('INSERT INTO ' . $table_prefix . 'wiki_article ' . "
              "$db->sql_build_array('INSERT', ['article_title' => " + json.dumps(title) + ", 'article_url' => "
              + json.dumps(title) + ", 'article_text' => $t, 'bbcode_uid' => $uid, 'bbcode_bitfield' => $bit, "
              "'article_approved' => 1, 'article_user_id' => 2, 'article_last_edit' => time(), "
              "'article_time_created' => time(), 'article_sources' => '', 'article_description' => '']));",
              check=True)
    b.php(store("See [[Second article]].\n[code][[Not a link]][/code]")
          + "$db->sql_query('UPDATE ' . POSTS_TABLE . ' SET ' . $db->sql_build_array('UPDATE', "
            "['post_text' => $t, 'bbcode_uid' => $uid, 'bbcode_bitfield' => $bit]) . ' WHERE post_id = 1');",
          check=True)
    b.purge_cache()

    admin = requests.Session()
    sid = b.login(admin)
    first = admin.get(f"{b.base}/app.php/wiki/First%20article").text
    ok(re.search(r'<a class="wikilink" href="[^"]*/wiki/Second%20article[^"]*"', first),
       "[[Second article]] links to the existing article")
    ok(re.search(r'<a class="wikilink wikilink-new" href="[^"]*/wiki/Missing%20page[^"]*"[^>]*>this one</a>', first),
       "[[Missing page|this one]] shows its own text and is marked as a new article")
    second = admin.get(f"{b.base}/app.php/wiki/Second%20article").text
    ok("What links here" in second and re.search(r'href="[^"]*/wiki/First%20article[^"]*">First article</a>', second),
       "Second article lists First article under What links here")
    ok("What links here" not in first, "an article nothing links to has no What links here list")
    topic = admin.get(f"{b.base}/viewtopic.php?t=1").text
    ok(re.search(r'<a class="wikilink" href="[^"]*/wiki/Second%20article', topic),
       "[[ ]] in a forum post links to the wiki article")
    ok("[[Not a link]]" in topic, "[[ ]] inside a code block stays plain text")
    posting = admin.get(f"{b.base}/posting.php?mode=reply&t=1&sid={sid}").text
    ok("bbcode-wikilink" in posting, "the posting editor has the wiki link button")


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


def check_posteripinviewtopic(b: Board) -> None:
    # The install's welcome post comes from 127.0.0.1, which the extension hides.
    # The lookup really asks ip-api.com about this documentation address, but only
    # the status code is checked, so the result doesn't depend on that service.
    b.sql("UPDATE phpbb_posts SET poster_ip = '203.0.113.7' WHERE post_id = 1")
    forum = b.sql("SELECT forum_id FROM phpbb_posts WHERE post_id = 1")[0][0]
    mod_pw, member_pw = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    mod_id = b.add_user("pipmod", mod_pw)
    b.add_user("pipmember", member_pw)
    # Standard Moderator in the welcome post's forum only, not a global moderator
    role = b.sql("SELECT role_id FROM phpbb_acl_roles WHERE role_name = 'ROLE_MOD_STANDARD'")[0][0]
    b.sql("INSERT INTO phpbb_acl_users (user_id, forum_id, auth_option_id, auth_role_id, auth_setting) "
          "VALUES (?, ?, 0, ?, 0)", (mod_id, forum, role))
    b.sql("UPDATE phpbb_users SET user_permissions = ''")
    b.purge_cache()
    lookup = f"{b.base}/app.php/freegeoip/1"
    ajax = {"X-Requested-With": "XMLHttpRequest"}

    def sees_ip(s: requests.Session) -> bool:
        return "203.0.113.7" in s.get(f"{b.base}/viewtopic.php?t=1").text

    for label, user, pw, allowed in (("administrator", None, None, True), ("moderator of the forum", "pipmod", mod_pw, True),
                                      ("member", "pipmember", member_pw, False)):
        s = requests.Session()
        b.login(s, user, pw)
        if allowed:
            ok(sees_ip(s), f"{label} sees the poster's IP")
            ok(s.get(lookup, headers=ajax).status_code == 200, f"{label} can look the IP up")
        else:
            ok(not sees_ip(s), f"{label} doesn't see the poster's IP")
            ok(s.get(lookup, headers=ajax).status_code == 403, f"{label} gets 403 from the lookup")


def check_printallposts(b: Board) -> None:
    # A reply, so the topic has two posts, and an image attached to the welcome post
    forum = b.sql("SELECT forum_id FROM phpbb_posts WHERE post_id = 1")[0][0]
    now = int(time.time())
    b.sql("INSERT INTO phpbb_posts (topic_id, forum_id, poster_id, poster_ip, post_time, post_visibility, "
          "post_subject, post_text, bbcode_uid, bbcode_bitfield) "
          "VALUES (1, ?, 2, '127.0.0.1', ?, 1, 'Re: probe', '<t>Second probe post</t>', '', '')", (forum, now))
    probe = b.root / "files" / "2_printallprobe"
    shutil.copy(b.root / "images/smilies/icon_e_smile.gif", probe)
    try:
        b.sql("INSERT INTO phpbb_attachments (post_msg_id, topic_id, in_message, poster_id, is_orphan, "
              "physical_filename, real_filename, download_count, attach_comment, extension, mimetype, filesize, "
              "filetime, thumbnail) VALUES (1, 1, 0, 2, 0, '2_printallprobe', 'printall-probe.gif', 0, '', 'gif', "
              "'image/gif', ?, ?, 0)", (probe.stat().st_size, now))
        b.sql("UPDATE phpbb_posts SET post_attachment = 1 WHERE post_id = 1")
        b.sql("UPDATE phpbb_topics SET topic_attachment = 1, topic_posts_approved = 2 WHERE topic_id = 1")
        b.sql("UPDATE phpbb_config SET config_value = '1' WHERE config_name = 'posts_per_page'")
        b.purge_cache()
        admin = requests.Session()
        b.login(admin)
        ok("view=printall" in admin.get(f"{b.base}/viewtopic.php?t=1").text, "Print all link in the Topic tools menu")
        body = admin.get(f"{b.base}/viewtopic.php?t=1&view=printall").text
        ok("Second probe post" in body and "This is an example post" in body,
           "with one post per page, the print page still shows both posts")
        ok("printall-probe.gif" in body, "the print page shows the post's attachment")
    finally:
        probe.unlink(missing_ok=True)


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


def check_removeextension(b: Board) -> None:
    # File attachments on the first post; the download link text should lose only
    # the last extension (removeextension#5: "annual.report.zip" used to become "annual").
    names = {"annual.report.zip": "annual.report", "archive.tar.gz": "archive.tar", "probe.zip": "probe"}
    upload = b.root / "files"
    now = int(time.time())
    try:
        for i, real in enumerate(names):
            physical = upload / f"2_removeextprobe{i}"
            physical.write_bytes(b"probe")
            b.sql("INSERT INTO phpbb_attachments (post_msg_id, topic_id, in_message, poster_id, is_orphan, "
                  "physical_filename, real_filename, download_count, attach_comment, extension, mimetype, "
                  "filesize, filetime, thumbnail) VALUES (1, 1, 0, 2, 0, ?, ?, 0, '', ?, "
                  "'application/octet-stream', 5, ?, 0)", (physical.name, real, real.rsplit(".", 1)[1], now))
        b.sql("UPDATE phpbb_posts SET post_attachment = 1 WHERE post_id = 1")
        b.sql("UPDATE phpbb_topics SET topic_attachment = 1 WHERE topic_id = 1")
        b.purge_cache()
        admin = requests.Session()
        b.login(admin)
        body = admin.get(f"{b.base}/viewtopic.php?t=1").text
        for real, shown in names.items():
            ok(re.search(rf">\s*{re.escape(shown)}\s*</a>", body), f"{real} is shown as {shown}")
        ok(not re.search(r">\s*annual\s*</a>", body), "annual.report.zip is not cut at its first dot")
    finally:
        b.sql("DELETE FROM phpbb_attachments WHERE physical_filename LIKE '2_removeextprobe%'")
        b.sql("UPDATE phpbb_posts SET post_attachment = 0 WHERE post_id = 1")
        b.sql("UPDATE phpbb_topics SET topic_attachment = 0 WHERE topic_id = 1")
        for i in range(len(names)):
            (upload / f"2_removeextprobe{i}").unlink(missing_ok=True)


def check_tlink(b: Board) -> None:
    # The admin gets the "can use topic link" permission, then posts a topic with a link.
    forum = b.sql("SELECT forum_id FROM phpbb_posts WHERE post_id = 1")[0][0]
    option = b.sql("SELECT auth_option_id FROM phpbb_acl_options WHERE auth_option = 'u_phpbbstudio_tlink'")[0][0]
    b.sql("INSERT INTO phpbb_acl_users (user_id, forum_id, auth_option_id, auth_role_id, auth_setting) "
          "VALUES (2, 0, ?, 0, 1)", (option,))
    b.sql("UPDATE phpbb_users SET user_permissions = ''")
    b.purge_cache()
    url = "https://www.example.com/tlink-probe"
    admin = requests.Session()
    sid = b.login(admin)
    page_url = f"{b.base}/posting.php?mode=post&f={forum}&sid={sid}"
    page = admin.get(page_url).text
    ok('name="tlink"' in page, "the new-topic form has the Link field")
    action, fields = form_fields(page, "postform")
    title = "tlink probe " + secrets.token_hex(4)  # unique, so a rerun on the same board reads its own topic
    post_back(admin, b.base, page_url, action, {**fields, "subject": title,
                                                "message": "tlink probe message", "tlink": url, "post": "Submit"})
    rows = b.sql("SELECT tlink, topic_status FROM phpbb_topics WHERE topic_title = ?", (title,))
    ok(rows and rows[0][0] == url, "the posted topic stores its link")
    ok(rows and str(rows[0][1]) == "1", "a topic with a link is locked")
    # submit_post() from code that, unlike posting.php, leaves out topic_first_post_id and post_id
    out = b.php("set_error_handler(function ($no, $msg) { echo 'PHP-WARNING: ' . $msg . \"\\n\"; return true; }, "
                "E_WARNING | E_NOTICE);"  # core's own deprecation notices aren't tlink's
                "include_once 'includes/functions_posting.php';"
                "$data = ['forum_id' => " + str(int(forum)) + ", 'topic_id' => 0, 'icon_id' => 0, 'poster_id' => 2, "
                "'enable_bbcode' => true, 'enable_smilies' => true, 'enable_urls' => true, 'enable_sig' => false, "
                "'message' => 'tlink submit_post probe', 'message_md5' => md5('tlink submit_post probe'), "
                "'bbcode_uid' => '', 'bbcode_bitfield' => '', 'enable_indexing' => true, 'notify' => false, "
                "'notify_set' => false, 'post_edit_locked' => false, 'topic_title' => 'tlink submit_post probe', "
                "'topic_time_limit' => 0, 'forum_name' => '', 'force_approved_state' => ITEM_APPROVED];"
                "$poll = []; submit_post('post', 'tlink submit_post probe', 'admin', POST_NORMAL, $poll, $data);"
                "echo 'TOPIC ' . $data['topic_id'] . \"\\n\";", check=True)
    warnings = [line for line in out.splitlines() if line.startswith("PHP-WARNING")]
    ok("TOPIC " in out and not warnings,
       "submit_post() without topic_first_post_id/post_id posts without warnings"
       + (f": {warnings[0][:150]}" if warnings else ""))


def check_activity24hours(b: Board) -> None:
    def block(body: str) -> str:
        m = re.search(r'<div class="stat-block 24stats">(.*?)</div>', body, re.S)
        return m.group(1) if m else ""

    admin = requests.Session()
    b.login(admin)
    page = admin.get(f"{b.base}/index.php")
    ok(page.status_code == 200 and "admin" in block(page.text), "the admin sees the 24-hour block, listing admin")
    ok(not block(requests.get(f"{b.base}/index.php").text), "guests don't see the 24-hour block by default")
    # Guests see it once given its permission (the install only grants it to user, moderator and admin roles)
    guests = b.sql("SELECT group_id FROM phpbb_groups WHERE group_name = 'GUESTS'")[0][0]
    option = b.sql("SELECT auth_option_id FROM phpbb_acl_options WHERE auth_option = 'u_a24hrs_view'")[0][0]
    b.sql("INSERT INTO phpbb_acl_groups (group_id, forum_id, auth_option_id, auth_role_id, auth_setting) "
          "VALUES (?, 0, ?, 0, 1)", (guests, option))
    b.sql("UPDATE phpbb_users SET user_permissions = ''")
    b.purge_cache()
    ok(block(requests.get(f"{b.base}/index.php").text), "guests given u_a24hrs_view see the 24-hour block")
    b.sql("DELETE FROM phpbb_acl_groups WHERE group_id = ? AND auth_option_id = ?", (guests, option))
    b.sql("UPDATE phpbb_users SET user_permissions = ''")
    b.purge_cache()
    # The listener takes hidebots' listener as an optional service; a wrong type hint
    # made every index page fail with a TypeError while hidebots was enabled.
    if (b.root / "ext/rmcgirr83/hidebots/composer.json").is_file():
        was_enabled = b.sql("SELECT ext_active FROM phpbb_ext WHERE ext_name = 'rmcgirr83/hidebots'")
        enabled_here = not was_enabled or str(was_enabled[0][0]) != "1"
        if enabled_here:
            b.cli("extension:enable", "rmcgirr83/hidebots")
        b.purge_cache()
        try:
            guest_page = requests.get(f"{b.base}/index.php")
            admin_page = admin.get(f"{b.base}/index.php")
            ok(guest_page.status_code == 200 and admin_page.status_code == 200,
               f"with hidebots enabled the index still loads (guest {guest_page.status_code}, "
               f"admin {admin_page.status_code})")
            ok(block(admin_page.text), "with hidebots enabled the admin still sees the 24-hour block")
        finally:
            if enabled_here:
                b.cli("extension:disable", "rmcgirr83/hidebots")
                b.purge_cache()
    else:
        print("  [skip] hidebots isn't on this board; the optional-service check needs it")


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
    mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%adduser%' AND module_mode = 'main'")[0][0]
    page_url = f"{b.base}/adm/index.php?i={mid}&sid={sid}"
    page = admin.get(page_url).text
    action, fields = form_fields(page)
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
    if 'name="default_group"' in page:
        check_adduser_default_group(b, admin, page_url)
    if b.sql("SELECT 1 FROM phpbb_modules WHERE module_basename LIKE '%adduser%' AND module_mode = 'csv'"):
        check_adduser_csv(b, admin, sid)
    if b.sql("SELECT 1 FROM phpbb_config WHERE config_name = 'adduser_admin_copy'"):
        check_adduser_admin_copy(b, admin, sid)
    language = b.root / "ext/phpbbmodders/adduser/language/en/info_acp_adduser.php"
    if language.is_file() and "ADDUSER_PASSWORD_REQUIRED" in language.read_text():
        check_adduser_no_email(b, admin, sid, page_url)


def check_adduser_no_email(b: Board, admin: requests.Session, sid: str, page_url: str) -> None:
    """adduser with board email off: a password is required, and the account is active at once.

    Runs with admin activation on, the setting that used to send admins an
    activation notice for an account that was already active. Restores the
    settings it changes.
    """
    saved = dict(b.sql("SELECT config_name, config_value FROM phpbb_config WHERE config_name IN "
                       "('email_enable', 'require_activation')"))
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'email_enable'")
    b.sql("UPDATE phpbb_config SET config_value = '2' WHERE config_name = 'require_activation'")
    b.purge_cache()
    try:
        page = admin.get(page_url).text
        ok("Board email is disabled" in page and 'name="activate"' not in page,
           "email off: the page says no email is sent and offers no activation box")

        def add(name: str, password: str) -> str:
            action, fields = form_fields(admin.get(page_url).text)
            return post_back(admin, f"{b.base}/adm", page_url, action, {
                **fields, "username": name, "email": f"{name}@example.com", "new_password": password,
                "password_confirm": password, "lang": "en", "group": "0", "submit": "Submit"})

        body = add("noemailblank", "")
        ok("enter a password" in body and not b.sql("SELECT 1 FROM phpbb_users WHERE username_clean = 'noemailblank'"),
           "email off: a blank password is refused instead of generating one nobody learns")
        body = add("noemailprobe", "Probe-pw-12345")
        row = b.sql("SELECT user_id, user_type FROM phpbb_users WHERE username_clean = 'noemailprobe'")
        ok(row and str(row[0][1]) == "0" and "no email was sent" in body,
           "email off: the account is created active and the message says no email was sent")
        if row:
            notices = b.sql("SELECT 1 FROM phpbb_notifications n JOIN phpbb_notification_types t "
                            "ON t.notification_type_id = n.notification_type_id "
                            "WHERE t.notification_type_name = 'notification.type.admin_activate_user' "
                            "AND n.item_id = ?", (row[0][0],))
            ok(not notices, "email off: admins get no activation notice for the already active account")
        mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%adduser%' "
                    "AND module_mode = 'csv'")[0][0]
        csv_url = f"{b.base}/adm/index.php?i={mid}&sid={sid}"
        _, fields = form_fields(admin.get(csv_url).text, "adduser_csv")
        time.sleep(1.1)
        r = admin.post(csv_url, data={**fields, "group": "0", "preview": "1"},
                       files={"csv_file": ("users.csv", b"username,email\nnoemailcsv,noemailcsv@example.com\n",
                                           "text/csv")}).text
        ok("this row needs a password" in page_text(r) and "No rows are ready" in page_text(r),
           "email off: a CSV row without a password is skipped")
    finally:
        for name, value in saved.items():
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, name))
        b.purge_cache()


def check_adduser_default_group(b: Board, admin: requests.Session, page_url: str) -> None:
    """adduser's default-group choice: Registered users, Newly registered users or the chosen group.

    Runs with board email off and the new-member post limit on, and restores
    the settings it changes.
    """
    saved = dict(b.sql("SELECT config_name, config_value FROM phpbb_config WHERE config_name IN "
                       "('email_enable', 'new_member_post_limit', 'new_member_group_default')"))
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'email_enable'")
    b.sql("UPDATE phpbb_config SET config_value = '3' WHERE config_name = 'new_member_post_limit'")
    try:
        groups = dict(b.sql("SELECT group_name, group_id FROM phpbb_groups WHERE group_name IN "
                            "('REGISTERED', 'NEWLY_REGISTERED', 'GLOBAL_MODERATORS')"))

        def add(name: str, extra: dict) -> str:
            page = admin.get(page_url).text
            action, fields = form_fields(page)
            return post_back(admin, f"{b.base}/adm", page_url, action, {
                **fields, "username": name, "email": f"{name}@example.com", "new_password": "Probe-pw-12345",
                "password_confirm": "Probe-pw-12345", "lang": "en", "group": "0", "new_user": "0",
                "submit": "Submit", **extra})

        def default_of(name: str) -> str:
            row = b.sql("SELECT group_id FROM phpbb_users WHERE username_clean = ?", (name,))
            return str(row[0][0]) if row else ""

        for setting, expected in (("0", "registered"), ("1", "newly_registered")):
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = 'new_member_group_default'",
                  (setting,))
            b.purge_cache()
            page = admin.get(page_url).text
            ok(re.search(r'value="%s"[^>]*checked' % expected, page),
               f"default group starts out as {expected} when the board's NRU-default setting is {setting}")

        add("dgprobenru", {"default_group": "newly_registered"})
        new = b.sql("SELECT user_new FROM phpbb_users WHERE username_clean = 'dgprobenru'")
        ok(default_of("dgprobenru") == str(groups["NEWLY_REGISTERED"]) and new and str(new[0][0]) == "1",
           "Newly registered users as default: set, and the account marked as a new user")
        add("dgprobereg", {"default_group": "registered", "new_user": "1"})
        ok(default_of("dgprobereg") == str(groups["REGISTERED"]),
           "Registered users as default wins over the board's NRU-default setting")
        add("dgprobegrp", {"default_group": "group", "group": str(groups["GLOBAL_MODERATORS"])})
        ok(default_of("dgprobegrp") == str(groups["GLOBAL_MODERATORS"]), "the chosen group as default")
        r = add("dgprobenone", {"default_group": "group"})
        ok("Choose a group above" in page_text(r) and not default_of("dgprobenone"),
           "the chosen group as default without a group is refused")
    finally:
        ids = [str(row[0]) for row in b.sql("SELECT user_id FROM phpbb_users WHERE username_clean LIKE 'dgprobe%'")]
        if ids:
            b.sql(f"DELETE FROM phpbb_user_group WHERE user_id IN ({','.join(ids)})")
            b.sql(f"DELETE FROM phpbb_users WHERE user_id IN ({','.join(ids)})")
        for name, value in saved.items():
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, name))
        b.purge_cache()


def check_adduser_admin_copy(b: Board, admin: requests.Session, sid: str) -> None:
    """adduser's administrator copy of welcome emails, through the CSV page.

    Board email goes to phpBB's queue (email_package_size on), so nothing is
    sent and the queued emails can be read from the queue file, which is
    emptied first and restored afterwards.
    """
    registration = admin.get(f"{b.base}/adm/index.php?i=acp_board&mode=registration&sid={sid}").text
    ok('name="config[adduser_admin_copy]"' in registration,
       "admin copy setting shown in ACP > Board settings > User registration settings")
    queue = b.root / "cache" / "production" / "queue.php"
    if not queue.parent.is_dir():
        queue = b.root / "cache" / "queue.php"
    saved_queue = queue.read_bytes() if queue.exists() else None
    saved = dict(b.sql("SELECT config_name, config_value FROM phpbb_config WHERE config_name IN "
                       "('email_enable', 'email_package_size', 'require_activation', 'adduser_admin_copy')"))
    b.sql("UPDATE phpbb_config SET config_value = '1' WHERE config_name = 'email_enable'")
    b.sql("UPDATE phpbb_config SET config_value = '20' WHERE config_name = 'email_package_size'")
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'require_activation'")
    admin_email = b.sql("SELECT user_email FROM phpbb_users WHERE username_clean = 'admin'")[0][0]
    mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%adduser%' AND module_mode = 'csv'")[0][0]
    url = f"{b.base}/adm/index.php?i={mid}&sid={sid}"

    def create(rows: str) -> str:
        queue.unlink(missing_ok=True)
        _, fields = form_fields(admin.get(url).text, "adduser_csv")
        time.sleep(1.1)
        r = admin.post(url, data={**fields, "group": "0", "preview": "1"},
                       files={"csv_file": ("users.csv", ("username,email\n" + rows).encode(), "text/csv")}).text
        _, create_fields = form_fields(r, "adduser_csv_create")
        time.sleep(1.1)
        admin.post(url, data={**create_fields, "create": "1"})
        return queue.read_text(errors="replace") if queue.exists() else ""

    try:
        for value, label in (("0", "off"), ("1", "on")):
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = 'adduser_admin_copy'", (value,))
            b.purge_cache()
            n = 2 if value == "1" else 1
            rows = "".join(f"copyprobe{label}{i},copyprobe{label}{i}@example.com\n" for i in range(n))
            text = create(rows)
            copies = text.count("Accounts created on")
            if value == "0":
                ok("copyprobeoff0@example.com" in text and copies == 0,
                   "admin copy off: the welcome email is queued, no copy")
                continue
            ok(copies == 1 and admin_email in text, "admin copy on: one copy for the whole file, to the admin")
            body = re.search(r"This is your copy.*?not included in this copy", text, re.S)
            ok(body and "copyprobeon0" in body.group(0) and "copyprobeon1" in body.group(0),
               "admin copy lists every created account")
            ok(body and "reset_password" not in body.group(0) and "mode=activate" not in body.group(0),
               "admin copy has no set-password or activation link")
    finally:
        ids = [str(row[0]) for row in b.sql("SELECT user_id FROM phpbb_users WHERE username_clean LIKE 'copyprobe%'")]
        if ids:
            b.sql(f"DELETE FROM phpbb_user_group WHERE user_id IN ({','.join(ids)})")
            b.sql(f"DELETE FROM phpbb_users WHERE user_id IN ({','.join(ids)})")
        for name, value in saved.items():
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, name))
        queue.unlink(missing_ok=True)
        if saved_queue is not None:
            queue.write_bytes(saved_queue)
        b.purge_cache()


def page_text(html: str) -> str:
    """Visible text of a page, whitespace collapsed, for matching messages."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def check_adduser_csv(b: Board, admin: requests.Session, sid: str) -> None:
    """adduser's "Add users from CSV" page: upload, check rows, create, summary.

    Runs with board email off (no mail is sent) and restores the settings it
    changes. The 50-row run guards against PHP's time limit: hashing each
    password with phpBB's default algorithm is slow on purpose.
    """
    saved = dict(b.sql("SELECT config_name, config_value FROM phpbb_config WHERE config_name IN "
                       "('email_enable', 'require_activation', 'allow_birthdays')"))
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name IN ('email_enable', 'require_activation')")
    b.sql("UPDATE phpbb_config SET config_value = '1' WHERE config_name = 'allow_birthdays'")
    b.purge_cache()
    try:
        mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%adduser%' "
                    "AND module_mode = 'csv'")[0][0]
        url = f"{b.base}/adm/index.php?i={mid}&sid={sid}"
        page = admin.get(url).text
        _, fields = form_fields(page, "adduser_csv")
        ok('enctype="multipart/form-data"' in page and "form_token" in fields,
           "CSV upload form takes a file and carries a form token")

        def upload(content: bytes, extra: dict = None) -> str:
            time.sleep(1.1)
            data = {**fields, "group": "0", "preview": "1", **(extra or {})}
            return admin.post(url, data=data, files={"csv_file": ("users.csv", content, "text/csv")}).text

        def confirm(data: dict) -> str:
            time.sleep(1.1)
            return admin.post(url, data={**data, "create": "1"}).text

        def count_csv_users() -> int:
            return int(b.sql("SELECT COUNT(*) FROM phpbb_users WHERE username_clean LIKE 'csvprobe%' "
                             "OR username_clean LIKE '%csvprobex%'")[0][0])

        r = upload(b"username,email,<b>phone</b>\n")
        ok("&lt;b&gt;phone" in r and "<b>phone</b>" not in r,
           "CSV: an unknown column is reported, its name escaped")
        time.sleep(1.1)
        ok("Choose a CSV file" in page_text(admin.post(url, data={**fields, "preview": "1"}).text),
           "CSV: a missing file is reported")

        group_id = str(b.sql("SELECT group_id FROM phpbb_groups WHERE group_name = 'GLOBAL_MODERATORS'")[0][0])
        csv = ("\ufeffUsername;Email;Password;Lang;Birthday\r\n"
               "csvprobea;csvprobea@example.com;Probe-pass-123;en;05-03-1990\r\n"
               "csvprobeb;csvprobeb@example.com;Probe-pass-123;;\r\n"
               "csvprobeb;csvprobeb2@example.com;;;\r\n"
               "csvprobec;not-an-email;;;\r\n"
               "admin;csvprobeadmin@example.com;;;\r\n"
               "csvprobed;csvprobed@example.com;;;1990-03-05\r\n"
               "csvprobee;csvprobee@example.com;;xx;\r\n"
               ";;;;\r\n"
               "<i>csvprobex</i>;csvprobex@example.com;Probe-pass-123;;\r\n").encode()
        log_before = int(b.sql("SELECT COUNT(*) FROM phpbb_log WHERE log_operation = 'LOG_USER_ADDED'")[0][0])
        r = upload(csv, {"group": group_id, "group_default": "1", "default_group": "group"})
        t = page_text(r)
        ok("3 rows are ready" in t and "5 rows are skipped" in t,
           "CSV check page: 3 rows ready, 5 skipped (semicolons, byte order mark, header in title case)")
        ok("The same username is on row 3" in t, "CSV check page: duplicate username in the file reported")
        ok("DD-MM-YYYY" in t and "The language you specified is not valid" in t,
           "CSV check page: wrong birthday format and unknown language reported")
        ok("&lt;i&gt;csvprobex&lt;/i&gt;" in r and "<i>csvprobex</i>" not in r,
           "CSV check page: markup in a username is escaped")
        ok("Probe-pass-123" not in r, "CSV check page: no plaintext password in the page")
        ok(count_csv_users() == 0, "CSV: checking creates nothing")

        _, create = form_fields(r, "adduser_csv_create")
        bad = confirm({**create, "csv_hash": "0" * 64})
        ok("were changed or are incomplete" in page_text(bad) and count_csv_users() == 0,
           "CSV: a tampered payload is refused and creates nothing")
        t = page_text(confirm(create))
        ok("3 accounts were created" in t and "5 rows are skipped" in t, "CSV summary: 3 created, 5 skipped")
        ok(count_csv_users() == 3, "CSV: exactly the 3 ready accounts exist")
        alice = b.sql("SELECT user_id, user_birthday, group_id FROM phpbb_users WHERE username_clean = 'csvprobea'")
        ok(alice and alice[0][1].replace(" ", "") == "5-3-1990" and str(alice[0][2]) == group_id,
           "CSV: birthday stored and the chosen group made the default group")
        log_after = int(b.sql("SELECT COUNT(*) FROM phpbb_log WHERE log_operation = 'LOG_USER_ADDED'")[0][0])
        ok(log_after - log_before == 3, "CSV: each created account was logged")
        ok("No accounts were created" in page_text(confirm(create)),
           "CSV: sending the same rows again creates nothing")

        def rows(n: int) -> bytes:
            return ("username,email,password\n" + "".join(
                f"csvprobebulk{i:03d},csvprobebulk{i:03d}@example.com,Probe-pass-123\n" for i in range(n))).encode()

        ok("more than 50 rows" in page_text(upload(rows(51))), "CSV: a 51-row file is refused")
        r = upload(rows(50))
        _, create = form_fields(r, "adduser_csv_create")
        ok("50 rows are ready" in page_text(r) and "50 accounts were created" in page_text(confirm(create)),
           "CSV: a full 50-row file is checked and created within PHP's time limit")
    finally:
        ids = [str(row[0]) for row in b.sql("SELECT user_id FROM phpbb_users WHERE username_clean LIKE "
                                            "'csvprobe%' OR username_clean LIKE '%csvprobex%'")]
        if ids:
            b.sql(f"DELETE FROM phpbb_user_group WHERE user_id IN ({','.join(ids)})")
            b.sql(f"DELETE FROM phpbb_users WHERE user_id IN ({','.join(ids)})")
        for name, value in saved.items():
            b.sql("UPDATE phpbb_config SET config_value = ? WHERE config_name = ?", (value, name))
        b.purge_cache()


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


def check_contactadmin(b: Board) -> None:
    def nav_links(html: str) -> tuple:
        """Return whether the header and footer navigation bars link to the contact page."""
        header = re.search(r'(?s)id="nav-main".*?id="nav-breadcrumbs"', html)
        footer = re.search(r'(?s)id="nav-footer".*', html)
        return tuple(bool(m and re.search(r"(app|index)\.php/contactadmin", m.group(0))) for m in (header, footer))

    contact_url = f"{b.base}/{b.route('contactadmin')}"
    # A fresh board can't send email, so deliver by private message; with
    # email delivery the contact page shows an error and the ACP won't save.
    # No visual confirmation, so a guest can send the form.
    b.sql("UPDATE phpbb_config SET config_value = '2' WHERE config_name = 'contactadmin_method'")
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'contactadmin_confirm'")
    b.purge_cache()
    ok(nav_links(requests.get(f"{b.base}/index.php").text) == (True, True),
       "guest sees the contact link in the header and footer navigation")
    ok('name="message"' in requests.get(contact_url).text, "guest contact form loads")

    # A guest's message is stored as a private message to the administrators
    guest = requests.Session()
    page = guest.get(contact_url).text
    action, fields = form_fields(page, "postform")
    before_pms = int(b.sql("SELECT COUNT(msg_id) FROM phpbb_privmsgs")[0][0])
    body = post_back(guest, b.base, contact_url, action, {
        **fields, "username": "Jane Example", "email": "jane@example.com", "email_confirm": "jane@example.com",
        "contact_subject": "Feature check", "message": "Probe message from the feature check.", "submit": "Submit"})
    ok("sent successfully" in body, "guest message sent through the contact form")
    ok(int(b.sql("SELECT COUNT(msg_id) FROM phpbb_privmsgs")[0][0]) == before_pms + 1,
       "the message was stored as a private message")

    if not b.sql("SELECT 1 FROM phpbb_config WHERE config_name = 'contactadmin_header_link'"):
        print("  [skip] header/footer link settings (not in this version)")
        return
    admin = requests.Session()
    sid = b.login(admin, acp=True)
    mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%contactadmin%' "
                "AND module_mode = 'configuration'")[0][0]
    page_url = f"{b.base}/adm/index.php?i={mid}&sid={sid}"

    def save(header: str, footer: str) -> None:
        """Submit the settings page as shown, changing only the two link settings."""
        html = admin.get(page_url).text
        form = re.search(r'(?s)<form[^>]*id="contactadmin".*?</form>', html).group(0)
        action, fields = form_fields(html, "contactadmin")
        for tag in re.findall(r'<input\b[^>]*>', form):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            if "name" in attrs and (attrs.get("type") in ("text", "number")
                                    or (attrs.get("type") == "radio" and "checked" in attrs)):
                fields[attrs["name"]] = attrs.get("value", "")
        fields.update(re.findall(r'(?s)<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', form))
        for name, options in re.findall(r'(?s)<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form):
            selected = re.search(r'<option value="([^"]*)"[^>]*selected', options)
            if selected:
                fields[name] = selected.group(1)
        post_back(admin, f"{b.base}/adm", page_url, action,
                  {**fields, "header_link": header, "footer_link": footer, "submit": "Submit"})

    def config(name: str) -> str:
        return b.sql("SELECT config_value FROM phpbb_config WHERE config_name = ?", (name,))[0][0]

    before = {r[0]: r[1] for r in b.sql("SELECT config_name, config_value FROM phpbb_config "
                                         "WHERE config_name LIKE 'contactadmin_%'")}
    save("0", "1")
    ok(config("contactadmin_header_link") == "0", "ACP: header link turned off")
    ok(nav_links(requests.get(f"{b.base}/index.php").text) == (False, True), "header link gone, footer link kept")
    save("1", "0")
    ok(nav_links(requests.get(f"{b.base}/index.php").text) == (True, False), "footer link gone, header link kept")
    save("0", "0")
    ok(nav_links(requests.get(f"{b.base}/index.php").text) == (False, False), "both links off: neither shown")
    ok('name="message"' in requests.get(contact_url).text,
       "both links off: the contact form still loads")
    after = {r[0]: r[1] for r in b.sql("SELECT config_name, config_value FROM phpbb_config "
                                        "WHERE config_name LIKE 'contactadmin_%'")}
    changed = sorted(k for k in before if before[k] != after.get(k)
                     and k not in ("contactadmin_header_link", "contactadmin_footer_link"))
    ok(not changed, "saving the settings changed no other setting" + (f": {changed}" if changed else ""))


def check_genders(b: Board) -> None:
    """Exercise the reported profile and UCP paths with the stock board style."""
    member = requests.Session()
    sid = b.login(member, acp=True)
    ucp = f"{b.base}/ucp.php?i=ucp_profile&mode=profile_info&sid={sid}"
    page = member.get(ucp, timeout=20)
    action, fields = form_fields(page.text, "ucp")
    ok(page.ok and 'name="user_gender"' in page.text and "form_token" in fields,
       "UCP profile displays the gender selector and form token")
    post_back(member, b.base, ucp, action, {**fields, "user_gender": "2", "submit": "Submit"})
    saved = b.sql("SELECT user_gender FROM phpbb_users WHERE user_id = 2")
    ok(saved and str(saved[0][0]) == "2", "UCP saves the selected gender")
    page = member.get(ucp, timeout=20)
    ok(re.search(r'<option\s+value="2"\s+selected=', page.text),
       "UCP displays the saved choice")
    profile = member.get(f"{b.base}/memberlist.php?mode=viewprofile&u=2", timeout=20)
    ok(profile.ok and 'class="fa fa-venus"' in profile.text, "member profile displays the saved gender icon")
    preview = member.get(f"{b.base}/{b.route('gender_id/2')}", timeout=20)
    ok(preview.ok and preview.json().get("gender") == "fa-venus", "gender preview route returns the correct icon")
    acp = member.get(f"{b.base}/adm/index.php?i=users&mode=profile&u=2&sid={sid}", timeout=20)
    ok(acp.ok and 'name="user_gender"' in acp.text, "ACP user profile displays the gender selector")


def check_autodbbackup(b: Board) -> None:
    """Save the ACP settings, run the backup cron three times and check retention and ACP Restore."""
    marker = "picoautodbbackup"  # kept by phpbbmodders/autodbbackup so older backups are still pruned
    on_storage = getattr(b, "version", "").startswith("4.")  # phpBB 4.0 keeps backups in its storage and backups table

    def backups() -> list:
        """Names of the backups phpBB knows about: the backups table on 4.0, store/ files on 3.3."""
        if on_storage:
            return sorted(r[0] for r in b.sql("SELECT filename FROM phpbb_backups"))
        out = b.php("foreach (glob($phpbb_root_path . 'store/backup_*') as $f) { echo basename($f), \"\\n\"; }")
        return sorted(line for line in out.splitlines() if line.startswith("backup_"))

    def config(name: str) -> str:
        rows = b.sql("SELECT config_value FROM phpbb_config WHERE config_name = ?", (name,))
        return rows[0][0] if rows else ""

    admin = requests.Session()
    sid = b.login(admin, acp=True)
    mid = b.sql("SELECT module_id FROM phpbb_modules WHERE module_basename LIKE '%autodbbackup%' "
                "AND module_mode = 'settings'")
    ok(mid, "ACP module is installed")
    if not mid:
        return
    page_url = f"{b.base}/adm/index.php?i={mid[0][0]}&sid={sid}"
    page = admin.get(page_url, timeout=20)
    ok(page.ok and 'id="acp_auto_db_backup"' in page.text and "BUY_ME" not in page.text,
       "ACP settings page loads")

    # On MySQL and MariaDB the cron also optimizes fragmented tables first
    optimize = "1" if isinstance(b, SeedBoard) and board_database(b) in ("mysql", "mariadb") else "0"
    # Two hours from now, on the board's own clock
    when = json.loads(b.php("echo json_encode(getdate(time() + 7200));"))
    action, fields = form_fields(page.text, "acp_auto_db_backup")
    body = post_back(admin, f"{b.base}/adm", page_url, action, {
        **fields, "auto_db_backup_enable": "1", "auto_db_backup_filetype": "text",
        "auto_db_backup_optimize": optimize, "auto_db_backup_gc": "2", "auto_db_backup_gc_unit": "hours",
        "auto_db_backup_copies": "2", "auto_db_backup_day": str(when["mday"]),
        "auto_db_backup_month": str(when["mon"]), "auto_db_backup_year": str(when["year"]),
        "auto_db_backup_hour": str(when["hours"]), "auto_db_backup_minute": str(when["minutes"]),
        "submit": "Submit"})
    ok("Settings changed" in body or "settings changed" in body.lower(), "ACP saves the settings")
    saved = {k: config(f"auto_db_backup_{k}") for k in ("enable", "gc", "gc_unit", "copies", "optimize")}
    ok(saved == {"enable": "1", "gc": "7200", "gc_unit": "hours", "copies": "2", "optimize": optimize},
       f"settings stored: {saved}")

    # A backup due in January must be selectable in December: next year is offered
    page = admin.get(page_url, timeout=20)
    ok(f'<option value="{when["year"] + 1}"' in page.text, "the next backup time offers next year too")
    # A file type the server can't write is refused and leaves the setting alone
    action, fields = form_fields(page.text, "acp_auto_db_backup")
    post_back(admin, f"{b.base}/adm", page_url, action, {
        **fields, "auto_db_backup_enable": "1", "auto_db_backup_filetype": "zip",
        "auto_db_backup_gc": "2", "auto_db_backup_gc_unit": "hours", "auto_db_backup_copies": "2",
        "auto_db_backup_day": str(when["mday"]), "auto_db_backup_month": str(when["mon"]),
        "auto_db_backup_year": str(when["year"]), "auto_db_backup_hour": str(when["hours"]),
        "auto_db_backup_minute": str(when["minutes"]), "submit": "Submit"})
    ok(config("auto_db_backup_filetype") == "text", "an unknown file type is refused")

    # A manual backup that retention must leave alone
    manual = "backup_1700000000_manualprobe00000000.sql"
    if on_storage:
        b.php(f"$s = $phpbb_container->get('storage.backup'); $s->write('{manual}', fopen('php://memory', 'rb'));"
              f"$db->sql_query('INSERT INTO ' . BACKUPS_TABLE . \" (filename) VALUES ('{manual}')\");", check=True)
    else:
        b.php(f"file_put_contents($phpbb_root_path . 'store/{manual}', '-- probe');", check=True)

    task = "phpbbmodders.autodbbackup.cron.task.auto_db_backup"
    for run in range(3):
        b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'auto_db_backup_last_gc'")
        b.purge_cache()
        out = b.cli("cron:run", task)
        ok("rror" not in out, f"cron run {run + 1} finished" + ("" if "rror" not in out else f": {out[-200:]}"))
        time.sleep(1.1)  # a new second, so each backup's timestamp differs
    auto = [f for f in backups() if marker in f]
    ok(len(auto) == 2, f"retention keeps the newest 2 automatic backups ({len(auto)} left)")
    ok(manual in backups(), "the manual backup is kept")
    ok(int(config("auto_db_backup_last_gc")) > 0, "the next backup time moved on after the run")
    logs = b.sql("SELECT COUNT(log_id) FROM phpbb_log WHERE log_operation = 'LOG_AUTO_DB_BACKUP'")
    ok(logs and int(logs[0][0]) >= 3, "each run is logged in the admin log")

    restore = admin.get(f"{b.base}/adm/index.php?i=acp_database&mode=restore&sid={sid}", timeout=20)
    # ACP Restore lists each backup by the SHA-1 of its file name
    ok(restore.ok and all(f'value="{hashlib.sha1(f.encode()).hexdigest()}"' in restore.text for f in auto),
       "ACP Restore lists the automatic backups")

    # Leave the board as it was: remove the probe and the backups this check made
    for name in auto + [manual]:
        if on_storage:
            b.php(f"$phpbb_container->get('storage.backup')->delete('{name}');"
                  f"$db->sql_query('DELETE FROM ' . BACKUPS_TABLE . \" WHERE filename = '{name}'\");")
        else:
            b.php(f"@unlink($phpbb_root_path . 'store/{name}');")
    if b.sql("SELECT 1 FROM phpbb_config WHERE config_name = 'auto_db_backup_directory'"):
        check_autodbbackup_directory(b, admin, page_url, when, task, marker, backups, config)
    b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'auto_db_backup_enable'")
    b.purge_cache()


def check_autodbbackup_directory(b: Board, admin: requests.Session, page_url: str, when: dict, task: str,
                                 marker: str, backups: Callable, config: Callable) -> None:
    """autodbbackup's optional private backup directory: ACP validation, cron writes there, retention, refusal."""
    def save(directory: str) -> None:
        action, fields = form_fields(admin.get(page_url, timeout=20).text, "acp_auto_db_backup")
        post_back(admin, f"{b.base}/adm", page_url, action, {
            **fields, "auto_db_backup_enable": "1", "auto_db_backup_filetype": "text",
            "auto_db_backup_optimize": "0", "auto_db_backup_gc": "2", "auto_db_backup_gc_unit": "hours",
            "auto_db_backup_copies": "2", "auto_db_backup_directory": directory,
            "auto_db_backup_day": str(when["mday"]), "auto_db_backup_month": str(when["mon"]),
            "auto_db_backup_year": str(when["year"]), "auto_db_backup_hour": str(when["hours"]),
            "auto_db_backup_minute": str(when["minutes"]), "submit": "Submit"})

    def custom_files(directory: str) -> list:
        out = b.php(f"foreach (glob('{directory}/backup_*') as $f) {{ echo basename($f), \"\\n\"; }}")
        return sorted(line for line in out.splitlines() if marker in line)

    def run_cron(times: int) -> None:
        for _ in range(times):
            b.sql("UPDATE phpbb_config SET config_value = '0' WHERE config_name = 'auto_db_backup_last_gc'")
            b.purge_cache()
            b.cli("cron:run", task)
            time.sleep(1.1)

    # A private directory the PHP process owns, outside the board and any web root
    directory = b.php("$d = sys_get_temp_dir() . '/adb-check-' . bin2hex(random_bytes(4));"
                      "echo (mkdir($d, 0700) ? realpath($d) : '');").strip()
    ok(directory, f"made a private test directory ({directory})")
    if not directory:
        return
    try:
        save(str(b.root / "store"))
        ok(config("auto_db_backup_directory") == "", "ACP refuses a directory inside the board")
        save(directory)
        ok(config("auto_db_backup_directory") == directory, "ACP saves a private directory outside the board")
        before = backups()
        run_cron(3)
        ok(len(custom_files(directory)) == 2, f"backups go to the custom directory, newest 2 kept "
                                              f"({len(custom_files(directory))} there)")
        ok(backups() == before, "no backup is left in phpBB's own backup location")
        # Opened to other users: refused at run time, logged, nothing written
        b.php(f"chmod('{directory}', 0777);", check=True)
        failed = int(b.sql("SELECT COUNT(log_id) FROM phpbb_log "
                           "WHERE log_operation = 'LOG_AUTO_DB_BACKUP_DIRECTORY_FAILED'")[0][0])
        kept = custom_files(directory)
        run_cron(1)
        ok(int(b.sql("SELECT COUNT(log_id) FROM phpbb_log "
                     "WHERE log_operation = 'LOG_AUTO_DB_BACKUP_DIRECTORY_FAILED'")[0][0]) == failed + 1
           and custom_files(directory) == kept, "a directory open to other users is refused and logged")
    finally:
        b.sql("UPDATE phpbb_config SET config_value = '' WHERE config_name = 'auto_db_backup_directory'")
        b.php(f"array_map('unlink', glob('{directory}/*')); @rmdir('{directory}');")
        b.purge_cache()


CHECKS = {
    "phpbbmodders/autodbbackup": check_autodbbackup,
    "phpbbmodders/genders": check_genders,
    "rmcgirr83/activity24hours": check_activity24hours,
    "phpbbmodders/contactadmin": check_contactadmin,
    "phpbbmodders/useridviewtopic": check_useridviewtopic,
    "phpbbmodders/wiki": check_wiki,
    "phpbbmodders/banlist": check_banlist,
    "phpbbmodders/bannerrotator": check_bannerrotator,
    "phpbbmodders/groupwarn": check_groupwarn,
    "phpbbmodders/documentation": check_documentation,
    "phpbbmodders/separatebots": check_separatebots,
    "phpbbmodders/posteripinviewtopic": check_posteripinviewtopic,
    "phpbbmodders/printallposts": check_printallposts,
    "phpbbmodders/reassignthumbs": check_reassignthumbs,
    "rmcgirr83/removeextension": check_removeextension,
    "phpbbstudio/tlink": check_tlink,
    "phpbbmodders/honeypot": check_honeypot,
    "phpbbmodders/adduser": check_adduser,
    "phpbbmodders/sfscompanion": check_sfscompanion,
}


def run_seed_board(args: argparse.Namespace) -> int:
    """Run the feature checks, then a disable/purge/enable round trip, on a seed-forum board."""
    board = SeedBoard(args.seed_board, args.port, args.seed_forum)
    print(f"== board {board.root} (phpBB {board.version}, {board_database(board)})")
    for spec in args.repos:
        repo = Path(spec.partition("@")[0]).expanduser().resolve()
        ext = json.loads((repo / "composer.json").read_text())["name"]
        print(f"== {ext} ({repo.name}, as mounted on the board)")
        active = board.sql("SELECT ext_active FROM phpbb_ext WHERE ext_name = ?", (ext,))
        if not active or str(active[0][0]) != "1":
            ok(False, f"{ext} is not enabled on the board; enable it with seed-forum")
            continue
        if ext in CHECKS:
            with board.serve():
                CHECKS[ext](board)
            errors = board.php_errors()
            ok(not errors, "no PHP errors logged" + (f": {errors[0][:200]}" if errors else ""))
        else:
            print("  [skip] no feature check for this extension")
        # The extension's migrations, reverted and run again on this database
        for step in ("disable", "purge", "enable"):
            out = board.cli(f"extension:{step}", ext)
            ok("Successfully" in out, f"extension:{step}" + ("" if "Successfully" in out else f": {out.strip()[-200:]}"))
        active = board.sql("SELECT ext_active FROM phpbb_ext WHERE ext_name = ?", (ext,))
        ok(active and str(active[0][0]) == "1", "enabled again after the round trip")

    failed = [label for good, label in results if not good]
    print(f"== {len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


def board_database(board: SeedBoard) -> str:
    """Name the database a seed-forum board runs on."""
    state = board.root / ".seed-forum-database.json"
    return json.loads(state.read_text())["engine"] if state.is_file() else "sqlite"


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
                    help="port for the temporary web server (default: 8083); with --seed-board, the board's "
                         "own server_port, so its generated links work")
    ap.add_argument("-s", "--seed-board", metavar="BOARD_ROOT",
                    help="run against an installed seed-forum board instead (any database seed-forum supports): "
                         "the extensions must already be enabled there; nothing is installed or reset, and after "
                         "the checks each extension is disabled, purged and enabled again through phpBB's CLI")
    ap.add_argument("--seed-forum", metavar="DIR", default=str(Path(__file__).resolve().parents[2] / "seed-forum"),
                    help="seed-forum checkout used with --seed-board (default: seed-forum next to this repository)")
    args = ap.parse_args()

    if args.seed_board:
        if args.needed:
            ap.error("--with can't be combined with --seed-board; enable required extensions with seed-forum")
        return run_seed_board(args)

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
