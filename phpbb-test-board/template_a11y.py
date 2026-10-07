#!/usr/bin/env python3
"""
Check a phpBB extension's or style's templates for icon-only controls without text.

Scans every .html template under the given checkouts (styles/ and adm/style/,
or a style's own template/ folder) for links and buttons whose only content
is an icon or image. Each such control needs:

  - a title attribute, for the tooltip sighted mouse users see, and
  - screen-reader text: a <span class="sr-only"> inside it or an aria-label.

In ACP templates use aria-label: the ACP style has no sr-only class, so a
span would be visible there. A title on the icon itself, phpBB's ACP
pattern, also counts as the tooltip.
A link around an image with alt text is described by that text and passes.

Both should come from a language string, not hard-coded text. Template
syntax ({L_FOO}, {{ lang('FOO') }}, <!-- IF -->, {% if %}) counts as text, so
it works on phpBB 3.3 and Twig templates alike. Uses only the standard
library and needs no test board.

Output: one line per problem, as FILE:LINE: message.
Exit status: 0 no problems, 1 problems found, 2 bad arguments.
"""
import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

CONTROLS = {"a", "button"}
ICON_TAGS = {"i", "img", "svg"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# Template logic that prints nothing: {% ... %}, {# ... #}.
TEMPLATE_LOGIC = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)


class Control:
    """One <a> or <button> being read, and what it contains."""

    def __init__(self, tag: str, attrs: dict, line: int) -> None:
        self.tag = tag
        self.attrs = attrs
        self.line = line
        self.has_icon = False
        self.has_described_image = False
        self.child_title = False
        self.visible_text = False
        self.screen_reader_text = False


class TemplateChecker(HTMLParser):
    """Collect icon-only controls that lack a tooltip or screen-reader text."""

    def __init__(self, acp: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.acp = acp
        self.problems: list = []
        self.controls: list = []
        self.sr_only_depth: list = []  # per open element: inside an sr-only element?

    def handle_starttag(self, tag: str, attr_list: list) -> None:
        attrs = {name: (value or "") for name, value in attr_list}
        parent_sr = bool(self.sr_only_depth and self.sr_only_depth[-1])
        in_sr = parent_sr or "sr-only" in attrs.get("class", "").split()
        if tag in CONTROLS:
            self.controls.append(Control(tag, attrs, self.getpos()[0]))
        elif self.controls:
            control = self.controls[-1]
            if tag in ICON_TAGS:
                if tag == "img" and attrs.get("alt", "").strip():
                    control.has_described_image = True
                else:
                    control.has_icon = True
                if attrs.get("title", "").strip():
                    control.child_title = True
        if tag not in VOID_TAGS:
            self.sr_only_depth.append(in_sr)

    def handle_startendtag(self, tag: str, attr_list: list) -> None:
        self.handle_starttag(tag, attr_list)
        if tag not in VOID_TAGS and self.sr_only_depth:
            self.sr_only_depth.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID_TAGS and self.sr_only_depth:
            self.sr_only_depth.pop()
        if tag in CONTROLS and self.controls and self.controls[-1].tag == tag:
            self.check(self.controls.pop())

    def handle_data(self, data: str) -> None:
        if not self.controls:
            return
        text = TEMPLATE_LOGIC.sub("", data).replace("\xa0", " ").strip()
        if not text:
            return
        control = self.controls[-1]
        if self.sr_only_depth and self.sr_only_depth[-1]:
            control.screen_reader_text = True
        else:
            control.visible_text = True

    def check(self, control: Control) -> None:
        """Record what an icon-only control is missing."""
        # An image with alt text describes the link itself, like visible text.
        if not control.has_icon or control.visible_text or control.has_described_image:
            return
        named = control.screen_reader_text or control.attrs.get("aria-label", "").strip() \
            or control.attrs.get("aria-labelledby", "").strip()
        # phpBB's ACP puts the title on the icon itself; that still gives a tooltip.
        missing = []
        if not control.attrs.get("title", "").strip() and not control.child_title:
            missing.append("a title (tooltip)")
        if not named:
            # The ACP style has no sr-only class, so a span there would show.
            missing.append("screen-reader text (aria-label; the ACP has no sr-only class)" if self.acp
                           else 'screen-reader text (<span class="sr-only"> or aria-label)')
        if missing:
            self.problems.append((control.line, f"icon-only <{control.tag}> without " + " or ".join(missing)))


def template_files(root: Path) -> list:
    """The .html templates of an extension or style checkout."""
    found = []
    for folder in ("styles", "adm/style", "template"):
        base = root / folder
        if base.is_dir():
            found.extend(p for p in base.rglob("*.html") if p.is_file())
    return sorted(set(found))


def check_file(path: Path) -> list:
    """Problems in one template, as (line, message) pairs."""
    checker = TemplateChecker(acp="adm/style" in path.as_posix())
    checker.feed(path.read_text(encoding="utf-8", errors="replace"))
    checker.close()
    return checker.problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Check a phpBB extension's or style's templates for icon-only "
                                             "links and buttons without a tooltip or screen-reader text.",
                                 epilog="Exit status: 0 no problems, 1 problems found, 2 bad arguments.")
    ap.add_argument("paths", nargs="+", metavar="DIR",
                    help="extension or style checkout to scan (its styles/, adm/style/ or template/ folder); "
                         "repeat for several")
    args = ap.parse_args()

    problems = 0
    for raw in args.paths:
        root = Path(raw).expanduser()
        if not root.is_dir():
            ap.error(f"{raw} is not a directory")
        for path in template_files(root):
            for line, message in check_file(path):
                print(f"{path}:{line}: {message}")
                problems += 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
