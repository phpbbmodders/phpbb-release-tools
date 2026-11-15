#!/usr/bin/env python3
"""
Add Titania demo data to a seed-forum board, for trying Titania Topic Transfer.

Adds an extensions category with four approved extension contributions to
Titania (the Customisation Database), and a public "Extension support" forum
holding support topics about them, written by the board's fixture users. The
topics are left in the forum, so a moderator can move them to the matching
contribution with Transfer to Titania support. The board admin is given
Titania's permissions so the admin login can do it.

The board must be a seed-forum board with phpbb/titania and
phpbbmodders/titaniatopictransfer enabled, for example one built with
db-matrix.sh --keep. It is changed in place, so run this on a disposable test
board only. Running it twice is refused, so the demo data is never doubled.

Exit status: 0 done, 1 the data could not be added, 2 bad arguments.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

from _board import SeedBoard  # noqa: E402

# Each contribution: name, short description, and the support topics posted about it in the forum.
# A topic is its title and its posts in order; the first post opens the topic.
CONTRIBUTIONS = [
    ("Topic Claim", "Lets moderators claim a topic so other moderators know who is looking after it.", [
        ("Topic Claim: claim icon not showing in the topic list", [
            "I enabled Topic Claim and claimed a topic, but the icon does not show in the forum's topic list. The notice on the topic page works.",
            "Which style are you using? The icon only shows to moderators, so check while logged in as one.",
            "Prosilver, logged in as a global moderator. Still nothing.",
            "Purging the cache in the ACP fixed it for me after updating.",
        ]),
        ("Topic Claim: can administrators take over a claim?", [
            "One of our moderators is away. Can an admin release their claims?",
            "Yes, with the admin permission to take over, transfer and release claims, from MCP » Claimed topics.",
        ]),
    ]),
    ("Feed Links", "Adds RSS feed links to the board index and to each forum.", [
        ("Feed Links: feed icon next to every forum is too large", [
            "Since installing Feed Links, the feed icons on the index are bigger than the forum icons. Is there a setting?",
            "There is no size setting; it uses the style's icon size. A line of CSS in your style can shrink it.",
            "Thanks, that worked.",
        ]),
    ]),
    ("See Notes", "Shows moderator notes about a member to other moderators in topics.", [
        ("See Notes: notes button missing for global moderators", [
            "The notes button only shows for administrators on our board, not for global moderators.",
            "Check the moderator permission for reading notes on the Global Moderators group.",
            "That was it, the role did not include it. Fixed now.",
            "Off topic, but does anyone know a good extension for member warnings?",
        ]),
    ]),
    ("Copy Links", "Adds buttons to copy a post's link or BBCode to the clipboard.", [
        ("Copy Links: copy button does nothing on mobile", [
            "On my phone the copy button shows but nothing is copied.",
            "Browsers only allow copying on secure pages. Is your board served over HTTPS?",
            "No, only HTTP for now. I will switch and report back.",
        ]),
    ]),
]

FIXTURE_PHP = r"""
$demo = json_decode(hex2bin($argv_demo), true);
$r = $db->sql_query("SELECT contrib_id FROM customisation_contribs WHERE contrib_name_clean = 'topic-claim'");
if ($db->sql_fetchrow($r)) { echo json_encode(['error' => 'the demo data is already on this board']); exit; }
$db->sql_freeresult($r);

// Titania's permissions for the board admin, given directly so no role has to change
$r = $db->sql_query('SELECT auth_option_id FROM ' . ACL_OPTIONS_TABLE . " WHERE auth_option " . $db->sql_like_expression('u_titania' . $db->get_any_char()));
$rows = [];
while ($o = $db->sql_fetchrow($r)) { $rows[] = ['user_id' => 2, 'forum_id' => 0, 'auth_option_id' => (int) $o['auth_option_id'], 'auth_role_id' => 0, 'auth_setting' => 1]; }
$db->sql_freeresult($r);
$db->sql_query('DELETE FROM ' . ACL_USERS_TABLE . ' WHERE user_id = 2 AND forum_id = 0 AND auth_option_id IN (' . implode(',', array_column($rows, 'auth_option_id') ?: [0]) . ')');
$db->sql_multi_insert(ACL_USERS_TABLE, $rows);
$auth->acl_clear_prefetch();

// A public forum for extension support, with the permissions of the board's first public forum
$r = $db->sql_query('SELECT forum_id FROM ' . FORUMS_TABLE . " WHERE forum_type = " . FORUM_POST . " AND forum_password = '' ORDER BY left_id");
$readers = $auth->acl_get_list(ANONYMOUS, 'f_read');
$model = 0;
while ($f = $db->sql_fetchrow($r)) { if (in_array(ANONYMOUS, $readers[(int) $f['forum_id']]['f_read'] ?? [])) { $model = (int) $f['forum_id']; break; } }
$db->sql_freeresult($r);
if (!$model) { echo json_encode(['error' => 'no forum guests can read to copy permissions from']); exit; }
$r = $db->sql_query('SELECT parent_id FROM ' . FORUMS_TABLE . ' WHERE forum_id = ' . $model); $parent = (int) $db->sql_fetchfield('parent_id'); $db->sql_freeresult($r);
if (!function_exists('copy_forum_permissions')) { include 'includes/functions_admin.php'; }
if (!function_exists('validate_range')) { include 'includes/functions_acp.php'; }
if (!class_exists('acp_forums')) { include 'includes/acp/acp_forums.php'; }
$user->add_lang('acp/forums');
$forums = new acp_forums();
$forum = ['parent_id' => $parent, 'forum_type' => FORUM_POST, 'type_action' => '', 'forum_status' => ITEM_UNLOCKED, 'forum_parents' => '',
    'forum_name' => 'Extension support', 'forum_link' => '', 'forum_link_track' => false,
    'forum_desc' => 'Questions about extensions listed in the Customisation Database.', 'forum_desc_uid' => '', 'forum_desc_options' => 7, 'forum_desc_bitfield' => '',
    'forum_rules' => '', 'forum_rules_uid' => '', 'forum_rules_options' => 7, 'forum_rules_bitfield' => '', 'forum_rules_link' => '',
    'forum_image' => '', 'forum_style' => 0, 'display_subforum_list' => true, 'display_subforum_limit' => false, 'display_on_index' => true,
    'forum_topics_per_page' => 0, 'enable_indexing' => true, 'enable_icons' => false, 'enable_prune' => false, 'enable_post_review' => true,
    'enable_quick_reply' => true, 'enable_shadow_prune' => false, 'prune_days' => 7, 'prune_viewed' => 7, 'prune_freq' => 1,
    'prune_old_polls' => false, 'prune_announce' => false, 'prune_sticky' => false, 'prune_shadow_days' => 7, 'prune_shadow_freq' => 1,
    'forum_password' => '', 'forum_password_confirm' => '', 'forum_password_unset' => false, 'forum_flags' => 0, 'forum_options' => 0,
    'show_active' => true];
$errors = $forums->update_forum_data($forum);
if ($errors) { echo json_encode(['error' => 'the forum could not be made: ' . implode(' ', $errors)]); exit; }
$forum_id = (int) $forum['forum_id'];
copy_forum_permissions($model, [$forum_id], false, false);
$auth->acl_clear_prefetch();

// An extensions category and the contributions. Titania's TEXT columns have no default, so every one is given.
$r = $db->sql_query('SELECT MAX(right_id) AS r FROM customisation_categories'); $right = (int) $db->sql_fetchfield('r'); $db->sql_freeresult($r);
$db->sql_query('INSERT INTO customisation_categories ' . $db->sql_build_array('INSERT', ['parent_id' => 0, 'left_id' => $right + 1, 'right_id' => $right + 2,
    'category_type' => 8, 'category_name' => 'Board tools', 'category_name_clean' => 'board-tools', 'category_desc' => '', 'category_contribs' => count($demo['contribs'])]));
$category = (int) $db->sql_nextid();
$r = $db->sql_query('SELECT user_id FROM customisation_authors WHERE user_id = 2');
$author = $db->sql_fetchrow($r);
$db->sql_freeresult($r);
if ($author) {
    $db->sql_query('UPDATE customisation_authors SET author_contribs = author_contribs + ' . count($demo['contribs']) . ' WHERE user_id = 2');
} else {
    $db->sql_query('INSERT INTO customisation_authors ' . $db->sql_build_array('INSERT', ['user_id' => 2, 'author_contribs' => count($demo['contribs']), 'author_desc' => '']));
}

// The fixture users who write the forum posts, in turn
$r = $db->sql_query('SELECT user_id FROM ' . USERS_TABLE . ' WHERE user_type = ' . USER_NORMAL . ' AND user_id <> 2 ORDER BY user_id', 6);
$posters = [];
while ($u = $db->sql_fetchrow($r)) { $posters[] = (int) $u['user_id']; }
$db->sql_freeresult($r);
if (!$posters) { $posters = [2]; }

require_once 'includes/functions_posting.php';
$out = ['forum' => $forum_id, 'contribs' => [], 'topics' => []];
$turn = 0; $clock = time() - 86400 * 3;
foreach ($demo['contribs'] as $c) {
    $db->sql_query('INSERT INTO customisation_contribs ' . $db->sql_build_array('INSERT', ['contrib_user_id' => 2, 'contrib_type' => 8,
        'contrib_name' => $c['name'], 'contrib_name_clean' => $c['clean'], 'contrib_desc' => $c['desc'],
        'contrib_status' => 2, 'contrib_visible' => 1, 'contrib_categories' => (string) $category, 'contrib_creation_time' => $clock, 'contrib_last_update' => $clock]));
    $contrib = (int) $db->sql_nextid();
    $db->sql_query('INSERT INTO customisation_contrib_in_categories ' . $db->sql_build_array('INSERT', ['contrib_id' => $contrib, 'category_id' => $category]));
    $out['contribs'][] = ['id' => $contrib, 'name' => $c['name'], 'clean' => $c['clean']];
    foreach ($c['topics'] as $t) {
        $topic_id = 0;
        foreach ($t['posts'] as $i => $text) {
            $poster = $i === 0 ? $posters[$turn % count($posters)] : $posters[($turn + $i) % count($posters)];
            $r = $db->sql_query('SELECT * FROM ' . USERS_TABLE . ' WHERE user_id = ' . $poster); $user->data = array_merge($user->data, $db->sql_fetchrow($r)); $db->sql_freeresult($r);
            $auth->acl($user->data);
            $message = $text; $uid = $bitfield = ''; $flags = 0; generate_text_for_storage($message, $uid, $bitfield, $flags, true, true, true);
            $clock += 3600;
            $data = ['forum_id' => $forum_id, 'topic_id' => $topic_id, 'topic_title' => $t['title'], 'icon_id' => 0, 'enable_bbcode' => true, 'enable_smilies' => true,
                'enable_urls' => true, 'enable_sig' => false, 'message' => $message, 'message_md5' => md5($message), 'bbcode_bitfield' => $bitfield, 'bbcode_uid' => $uid,
                'post_edit_locked' => 0, 'force_approved_state' => ITEM_APPROVED, 'enable_indexing' => true, 'notify' => false, 'notify_set' => false, 'post_time' => $clock];
            $poll = [];
            submit_post($topic_id ? 'reply' : 'post', $topic_id ? 'Re: ' . $t['title'] : $t['title'], '', POST_NORMAL, $poll, $data);
            $topic_id = (int) $data['topic_id'];
        }
        $out['topics'][] = ['id' => $topic_id, 'title' => $t['title'], 'contrib' => $c['name']];
        $turn++;
    }
}
echo json_encode($out);
"""


def clean_name(name: str) -> str:
    """Return Titania's URL name for a contribution: lower case words joined with hyphens."""
    return "-".join("".join(ch for ch in word if ch.isalnum()) for word in name.lower().split())


def main() -> int:
    ap = argparse.ArgumentParser(description="Add Titania demo extensions and forum support topics to a seed-forum "
                                             "board, for trying Titania Topic Transfer.",
                                 epilog="Exit status: 0 done, 1 the data could not be added, 2 bad arguments.")
    ap.add_argument("-b", "--board", required=True, metavar="DIR",
                    help="the seed-forum board folder, with phpbb/titania and phpbbmodders/titaniatopictransfer enabled")
    ap.add_argument("-s", "--seed-forum", default=str(Path(__file__).resolve().parents[2] / "seed-forum"), metavar="DIR",
                    help="seed-forum checkout (default: seed-forum next to this repository)")
    args = ap.parse_args()

    board = SeedBoard(args.board, 0, args.seed_forum)
    for ext in ("phpbb/titania", "phpbbmodders/titaniatopictransfer"):
        active = board.sql("SELECT ext_active FROM phpbb_ext WHERE ext_name = ?", (ext,))
        if not active or str(active[0][0]) != "1":
            print(f"error: {ext} is not enabled on {board.root}", file=sys.stderr)
            return 1

    demo = {"contribs": [{"name": name, "clean": clean_name(name), "desc": desc,
                          "topics": [{"title": title, "posts": posts} for title, posts in topics]}
                         for name, desc, topics in CONTRIBUTIONS]}
    encoded = json.dumps(demo).encode().hex()
    out = board.php(f"$argv_demo = '{encoded}';" + FIXTURE_PHP).strip().splitlines()
    result = json.loads(out[-1]) if out and out[-1].startswith("{") else {"error": " ".join(out)[-400:] or "no output"}
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    board.purge_cache()
    print(f"Forum: Extension support (forum {result['forum']})")
    for c in result["contribs"]:
        print(f"Contribution {c['id']}: {c['name']} (db/extension/{c['clean']})")
    for t in result["topics"]:
        print(f"Topic {t['id']}: {t['title']} -> {t['contrib']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
