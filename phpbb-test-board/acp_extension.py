#!/usr/bin/env python3
"""
Enable or disable a phpBB extension through the Administration Control Panel.

Does what an administrator does on Customise > Manage extensions: logs in,
re-authenticates for the ACP, opens the extension's Enable (or Disable)
confirmation, submits it, then follows each "in progress" page refresh phpBB
sends while it runs the extension's steps and migrations in time-limited
chunks, until phpBB reports success or an error. Use it on a served local
test board (seed-forum's nginx site, for example), for extensions whose
migrations need a logged-in user, and to time the web path of a long
migration.

Inputs:  the action (enable or disable), the extension name (vendor/name),
         --url (the board's address), and the admin login: seed-forum's
         public fixture admin by default, or --user plus a password in the
         PHPBB_ADMIN_PASSWORD environment variable.
Output:  one line per request, then a summary with the number of requests,
         the time taken and phpBB's final message.
Exit status: 0 when phpBB reports success, 1 when it reports an error or
stops without one, 2 for bad arguments.
"""
import argparse
import html
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

import requests  # noqa: E402

# seed-forum's public fixture admin; never a real board's
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "KbTest1234!"
# phpBB rejects a form submitted less than a second after it was made
FORM_WAIT = 1.1
REFRESH = re.compile(r'<meta http-equiv="refresh" content="\d+;\s*url=([^"]+)"', re.I)


def form_html(page: str, form_id: str = "", containing: str = "") -> str:
    """The form with id form_id, or the first form whose HTML contains `containing`."""
    for match in re.finditer(r"<form[^>]*>.*?</form>", page, re.S):
        form = match.group(0)
        opening = form[:form.index(">") + 1]
        if (form_id and f'id="{form_id}"' in opening) or (not form_id and containing in form):
            return form
    return ""


def form_fields(page: str, form_id: str = "", containing: str = "") -> dict:
    """The named inputs of a form (see form_html()) as a dict."""
    body = form_html(page, form_id, containing)
    fields = {}
    for tag in re.findall(r"<input[^>]*>", body):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[html.unescape(name.group(1))] = html.unescape(value.group(1)) if value else ""
    return fields


def message(page: str) -> str:
    """phpBB's message box text (success or error), or the page title."""
    for pattern in (r'<div class="(?:successbox|errorbox)[^"]*">(.*?)</div>', r"<h1>(.*?)</h1>", r"<title>(.*?)</title>"):
        found = re.search(pattern, page, re.S)
        if found:
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", found.group(1)))).strip()
    return ""


def session_id(session: requests.Session) -> str:
    """The phpBB session id from the cookie jar."""
    return next((c.value for c in session.cookies if c.name.endswith("_sid")), "")


def login(session: requests.Session, base: str, user: str, password: str) -> str:
    """Log in on the board, then re-authenticate for the ACP; return the session id.

    The session id goes in every URL: Python's cookie jar files cookies for a
    bare "localhost" under "localhost.local" and never sends them back, and
    phpBB accepts the id from the URL instead.
    """
    page = session.get(f"{base}/ucp.php?mode=login", timeout=60).text
    fields = form_fields(page, "login")
    time.sleep(FORM_WAIT)
    fields.update({"username": user, "password": password, "login": "Login"})
    page = session.post(f"{base}/ucp.php?mode=login", data=fields, timeout=60).text
    sid = session_id(session)
    if "mode=logout" not in page or not sid:
        raise SystemExit("error: could not log in; check the admin name and password")

    page = session.get(f"{base}/adm/index.php?sid={sid}", timeout=60).text
    if 'name="credential"' in page:
        form = form_html(page, containing='name="credential"')
        fields = form_fields(form, containing='name="credential"')
        password_field = next((name for name in fields if name.startswith("password_")), None)
        if not password_field:
            raise SystemExit("error: the ACP login form has no password field")
        fields[password_field] = password
        fields["login"] = "Login"
        action = re.search(r'<form[^>]*action="([^"]+)"', form)
        time.sleep(FORM_WAIT)
        page = session.post(urljoin(f"{base}/adm/", html.unescape(action.group(1))) if action else f"{base}/adm/index.php?sid={sid}",
                            data=fields, timeout=60).text
        sid = session_id(session) or sid
        page = session.get(f"{base}/adm/index.php?sid={sid}", timeout=60).text
    if 'id="acp"' not in page:
        raise SystemExit("error: could not open the ACP; is this user an administrator?")
    return sid


def main() -> int:
    ap = argparse.ArgumentParser(description="Enable or disable a phpBB extension through the ACP, as an admin would.",
                                 epilog="Exit status: 0 success, 1 phpBB reported an error or stopped, 2 bad arguments.")
    ap.add_argument("action", choices=["enable", "disable"], help="what to do with the extension")
    ap.add_argument("ext_name", help="the extension's name, vendor/name, as phpBB lists it")
    ap.add_argument("-u", "--url", default="http://localhost:8083",
                    help="the served board's address (default: %(default)s)")
    ap.add_argument("--user", default=DEFAULT_USER,
                    help="admin to log in as (default: seed-forum's fixture admin); give the password in PHPBB_ADMIN_PASSWORD")
    ap.add_argument("-m", "--max-requests", type=int, default=5000,
                    help="give up after this many page refreshes (default: %(default)s)")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+", args.ext_name):
        ap.error("ext_name must look like vendor/name")
    if args.user != DEFAULT_USER and not os.environ.get("PHPBB_ADMIN_PASSWORD"):
        ap.error("set PHPBB_ADMIN_PASSWORD for --user")
    password = os.environ.get("PHPBB_ADMIN_PASSWORD", DEFAULT_PASSWORD)
    base = args.url.rstrip("/")

    session = requests.Session()
    try:
        return run(session, base, args, password)
    except requests.RequestException as error:
        print(f"error: could not reach the board at {base}: {error}", file=sys.stderr)
        return 1


def run(session: requests.Session, base: str, args: argparse.Namespace, password: str) -> int:
    """Log in, then enable or disable the extension; return the exit status."""
    sid = login(session, base, args.user, password)

    started = time.monotonic()
    query = urlencode({"i": "acp_extensions", "mode": "main", "action": f"{args.action}_pre",
                       "ext_name": args.ext_name, "sid": sid})
    page = session.get(f"{base}/adm/index.php?{query}", timeout=60).text
    action = re.search(r'<form id="acp_extensions"[^>]*action="([^"]+)"', page)
    if not action:
        print(f"error: no {args.action} confirmation: {message(page)}", file=sys.stderr)
        return 1
    url = urljoin(f"{base}/adm/", html.unescape(action.group(1)))
    time.sleep(FORM_WAIT)
    response = session.post(url, data={args.action: "1"}, timeout=600)

    requests_made = 1
    while True:
        page = response.text
        print(f"request {requests_made}: HTTP {response.status_code}, {time.monotonic() - started:.1f}s - {message(page)[:120]}", flush=True)
        refresh = REFRESH.search(page)
        if response.status_code != 200 or not refresh:
            break
        if requests_made >= args.max_requests:
            print(f"error: still in progress after {requests_made} requests", file=sys.stderr)
            return 1
        response = session.get(urljoin(f"{base}/adm/", html.unescape(refresh.group(1))), timeout=600)
        requests_made += 1

    elapsed = time.monotonic() - started
    final = message(page)
    succeeded = response.status_code == 200 and 'class="successbox' in page and "IN_PROGRESS" not in final
    print(f"{'OK' if succeeded else 'FAILED'}: {args.action} {args.ext_name} took {requests_made} request(s), {elapsed:.1f}s - {final}")
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
