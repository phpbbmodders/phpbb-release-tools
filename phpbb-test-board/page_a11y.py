#!/usr/bin/env python3
"""
Run automated accessibility checks on pages of a local phpBB test board.

Each page is opened in Chromium (Playwright) at a desktop and a phone width,
as a guest or as the board admin with the ACP unlocked, and checked with:

- axe-core, limited to the WCAG 2.0, 2.1 and 2.2 A and AA rules: every
  violation is a failure; "incomplete" results (rules axe could not decide)
  are listed for review by hand and do not fail the run;
- reflow (WCAG 1.4.10): at the phone width the page must not scroll
  sideways;
- with --keys, keyboard use at the desktop width: Tab is pressed through
  the page's main content (#main in the ACP, #page-body on the board) and
  every stop is listed with its role and accessible name. A stop with no
  visible focus indicator (WCAG 2.4.7) is a failure.

axe-core is downloaded from the npm registry on first use, pinned to
AXE_VERSION and checked against the registry's published sha512 hash, then
kept in ~/.cache/phpbb-release-tools/. Nothing else is fetched.

An automated scan is not a full accessibility review: it cannot judge focus
order, wording or screen-reader output, so test keyboard use and the
accessibility tree of the controls that changed as well.

Exit status: 0 no violations, 1 a violation or a page failed to load,
2 bad arguments.
"""
import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv import ensure_venv  # noqa: E402

ensure_venv()

import requests  # noqa: E402
from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: E402

from _board import Board, SeedBoard  # noqa: E402

AXE_VERSION = "4.14.0"
AXE_INTEGRITY = "sha512-9WTZxEjsZ7b13TH8JPmbV2z8CHbl80/2hm3XPEG4JgNdQLK81IBRXmSxHfMAOkSqQeRxT/0dwNDz2GOm3zzpcQ=="
AXE_TARBALL = f"https://registry.npmjs.org/axe-core/-/axe-core-{AXE_VERSION}.tgz"
AXE_CACHE = Path.home() / ".cache/phpbb-release-tools" / f"axe-core-{AXE_VERSION}" / "axe.min.js"
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"]
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "phone": {"width": 390, "height": 844}}
TIMEOUT_MS = 30000


def axe_source() -> str:
    """Return axe.min.js, downloading and verifying the pinned release on first use."""
    if not AXE_CACHE.is_file():
        print(f"Downloading axe-core {AXE_VERSION} ...", file=sys.stderr)
        response = requests.get(AXE_TARBALL, timeout=60)
        response.raise_for_status()
        algorithm, expected = AXE_INTEGRITY.split("-", 1)
        actual = base64.b64encode(hashlib.new(algorithm, response.content).digest()).decode()
        if actual != expected:
            raise SystemExit(f"error: axe-core {AXE_VERSION} tarball does not match its published {algorithm} hash")
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            member = tar.extractfile("package/axe.min.js")
            if member is None:
                raise SystemExit("error: axe.min.js missing from the axe-core tarball")
            AXE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            AXE_CACHE.write_bytes(member.read())
    return AXE_CACHE.read_text()


def parse_page(spec: str) -> tuple:
    """Split LABEL=URL; URL is relative to the board root."""
    label, sep, url = spec.partition("=")
    if not sep or not label or not url:
        raise argparse.ArgumentTypeError(f"expected LABEL=URL, got {spec!r}")
    return label, url.lstrip("/")


FOCUS_STATE = """() => {
    const el = document.activeElement;
    if (!el || el === document.body) return null;
    const inside = !!el.closest('#main, #page-body');
    const style = getComputedStyle(el);
    // A focus indicator is any visible change between the focused and the
    // unfocused look: an outline or shadow, or a border, background or text
    // colour that only focus applies (phpBB's ACP styles fields that way).
    const props = ['outlineStyle', 'outlineWidth', 'outlineColor', 'boxShadow', 'borderTopColor',
                   'borderBottomColor', 'backgroundColor', 'color', 'textDecorationLine'];
    const focused = props.map(p => style[p]);
    el.blur();
    const plain = props.map(p => getComputedStyle(el)[p]);
    el.focus();
    const outlineOn = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
    const changed = focused.some((v, i) => v !== plain[i]);
    const outline = changed || (outlineOn && focused[0] !== 'none');
    const shadow = false;
    const labels = el.labels ? Array.from(el.labels).map(l => l.textContent.trim()).join(' / ') : '';
    const name = el.getAttribute('aria-label') || labels || el.value && ['submit', 'reset', 'button'].includes(el.type) && el.value
        || el.textContent.trim().slice(0, 60) || el.getAttribute('title') || '';
    const visible = el.offsetParent !== null || style.position === 'fixed';
    return {inside, outline: outline || shadow, visible,
            desc: `${el.tagName.toLowerCase()}${el.type ? '[' + el.type + ']' : ''}${el.id ? '#' + el.id : ''}`
                  + `${el.name ? ' name=' + el.name : ''}`, name: name.replace(/\\s+/g, ' ')};
}"""


def walk_keys(page, label: str) -> int:
    """Tab through the main content; list each stop; return the number of stops without a focus indicator."""
    failures = 0
    seen = set()
    for _ in range(150):
        page.keyboard.press("Tab")
        state = page.evaluate(FOCUS_STATE)
        if state is None or not state["inside"]:
            if seen:
                break
            continue
        key = state["desc"] + state["name"]
        if key in seen:
            break
        seen.add(key)
        flag = "ok" if state["outline"] else "FAIL"
        if not state["outline"]:
            failures += 1
        hidden = "" if state["visible"] else " (not visible on screen)"
        print(f"  [{flag}] {label} keys: {state['desc']} \"{state['name']}\"{hidden}"
              + ("" if state["outline"] else " - no visible focus indicator"))
    if not seen:
        failures += 1
        print(f"  [FAIL] {label} keys: Tab never reached the main content")
    return failures


def check_page(browser, board, axe: str, label: str, url: str, admin: tuple, keys: bool = False) -> tuple:
    """Check one page at every viewport; return (violation count, list of review notes)."""
    violations = 0
    notes = []
    sid, agent, cookies = admin
    for name, viewport in VIEWPORTS.items():
        options = {"viewport": viewport, "locale": "en-GB", "is_mobile": name == "phone",
                   "has_touch": name == "phone"}
        if sid:
            options["user_agent"] = agent
        context = browser.new_context(**options)
        if sid:
            context.add_cookies(cookies)
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        try:
            target = f"{board.base}/{url.replace('{sid}', sid)}"
            response = page.goto(target, wait_until="networkidle")
            if response is None or response.status >= 400:
                raise RuntimeError(f"HTTP {response.status if response else 'no response'}")
            page.add_script_tag(content=axe)
            result = page.evaluate("tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})", WCAG_TAGS)
            for item in result["violations"]:
                violations += 1
                targets = ", ".join(" ".join(node["target"]) for node in item["nodes"][:5])
                print(f"  [FAIL] {label} ({name}): {item['id']} ({item['impact']}): {item['help']} -> {targets}")
                print(f"         first element: {' '.join(item['nodes'][0]['html'].split())[:200]}")
            for item in result["incomplete"]:
                notes.append(f"{label} ({name}): {item['id']}: {item['help']} ({len(item['nodes'])} element(s))")
            if keys and name == "desktop":
                violations += walk_keys(page, label)
            if name == "phone":
                overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if overflow > 1:
                    violations += 1
                    print(f"  [FAIL] {label} ({name}): reflow: the page scrolls sideways by {overflow}px")
            if not result["violations"] and not (name == "phone" and overflow > 1):
                print(f"  [ok] {label} ({name}): no violations")
        except (PlaywrightError, RuntimeError) as e:
            violations += 1
            print(f"  [FAIL] {label} ({name}): {str(e).splitlines()[0][:200]}")
        finally:
            context.close()
    return violations, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="Run axe-core (WCAG 2.2 A/AA rules) and a reflow check on pages of a "
                                             "local phpBB test board, at desktop and phone widths.",
                                 epilog="Example: page_a11y.py -s ~/repos/seeded-board/adduser-33 -p 8120 -a "
                                        "'Add User=adm/index.php?i=-phpbbmodders-adduser-acp-adduser_module"
                                        "&mode=main&sid={sid}'. Exit status: 0 no violations, 1 a violation "
                                        "or a page failed, 2 bad arguments.")
    ap.add_argument("pages", nargs="+", type=parse_page, metavar="LABEL=URL",
                    help="page to check, URL relative to the board root; with --admin, {sid} in URL is "
                         "replaced by the admin's session id")
    ap.add_argument("-a", "--admin", action="store_true",
                    help="open the pages as the board admin, with the ACP unlocked (default: as a guest)")
    ap.add_argument("-k", "--keys", action="store_true",
                    help="also press Tab through each page's main content at the desktop width, list every stop "
                         "with its accessible name, and fail stops without a visible focus indicator")
    ap.add_argument("-s", "--seed-board", metavar="BOARD_ROOT",
                    help="installed seed-forum board to serve and check")
    ap.add_argument("--seed-forum", metavar="DIR", default=str(Path(__file__).resolve().parents[2] / "seed-forum"),
                    help="seed-forum checkout used with --seed-board (default: seed-forum next to this repository)")
    ap.add_argument("-b", "--board-dir",
                    help="board directory created by setup-board.sh, instead of --seed-board "
                         "(default: $PHPBB_TEST_BOARD)")
    ap.add_argument("-p", "--port", type=int, default=8083,
                    help="port for the temporary web server (default: 8083); with --seed-board, the board's "
                         "own server_port")
    args = ap.parse_args()

    board = SeedBoard(args.seed_board, args.port, args.seed_forum) if args.seed_board else Board(args.board_dir,
                                                                                                 args.port)
    axe = axe_source()
    print(f"== {board.root} on {board.base}, axe-core {AXE_VERSION}, rules: {', '.join(WCAG_TAGS)}")
    violations = 0
    notes: list = []
    with board.serve(), sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError:
            print("error: Playwright's Chromium is missing; run: python3 -m playwright install chromium",
                  file=sys.stderr)
            return 1
        try:
            admin = ("", "", [])
            if args.admin:
                session = requests.Session()
                sid = board.login(session, acp=True)
                # phpBB checks the browser string against the one that logged in
                admin = (sid, session.headers["User-Agent"],
                         [{"name": c.name, "value": c.value, "domain": "localhost", "path": "/"}
                          for c in session.cookies])
            for label, url in args.pages:
                count, page_notes = check_page(browser, board, axe, label, url, admin, args.keys)
                violations += count
                notes += page_notes
        finally:
            browser.close()
    errors = board.php_errors()
    if errors:
        violations += 1
        print(f"  [FAIL] PHP errors logged: {errors[0][:200]}")
    for note in notes:
        print(f"  [review] {note}")
    print(f"== {violations} violation(s), {len(notes)} result(s) to review by hand")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
