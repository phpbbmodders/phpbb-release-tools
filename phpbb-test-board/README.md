# phpbb-test-board

Tools for testing phpBB extensions and styles on a local phpBB 3.3 board, and for taking their documentation screenshots.

They build a board on SQLite and run an extension on it with PHP error logging on. This catches problems that validation and syntax checks miss, such as pages that crash at runtime, forms that can never submit, or an extension that can't be enabled on a fresh board.

**For local testing only.** Never expose the board to a network: its admin account and test users exist only for testing.

## Requirements

- `git`, `php` (with the `sqlite3` extension), `composer`, `curl`, `jq` and `openssl`
- Python 3 with `venv`
- ImageMagick's `convert`, for the reassignthumbs feature check

The Python scripts create their own virtualenv (`.venv` in this folder) on first run and install `requirements.txt` into it; nothing needs installing or activating by hand. `screenshots.py` also downloads Playwright's Chromium on its first run.

## Quick start

```bash
phpbb-test-board/setup-board.sh -d ~/phpbb-test-board
export PHPBB_TEST_BOARD=~/phpbb-test-board
phpbb-test-board/smoke_test.py path/to/extension --ref origin/main
phpbb-test-board/feature_checks.py path/to/extension@origin/main
```

Instead of setting `PHPBB_TEST_BOARD`, each script also takes `--board-dir DIR`. Every script has `--help`.

## The scripts

### `setup-board.sh`: build the board

```bash
phpbb-test-board/setup-board.sh -d DIR [-v VERSION] [-f]
```

Installs the current stable phpBB 3.3 release, or `--version`, with no extensions enabled. It writes:

| File | What it is |
|---|---|
| `DIR/phpbb/` | the board |
| `DIR/board.sqlite3` | its database |
| `DIR/board.clean.sqlite3` | a copy of the fresh database, so every run starts from the same state |
| `DIR/board.env` | the board's paths and the generated admin password (mode 600; the password is never printed) |

`--force` replaces an existing board in `DIR`.

### `smoke_test.py`: does the extension break anything?

```bash
phpbb-test-board/smoke_test.py path/to/extension [--ref REF]
```

Installs the extension from git and loads, as a guest and as the admin:
- the main board pages;
- the extension's routes;
- its ACP, MCP and UCP modules in every mode;
- its cron tasks.

It then disables the extension, deletes its data and enables it again. It reports any server error, empty page, phpBB debug notice or PHP error-log entry.

### `feature_checks.py`: does the extension's main feature work?

```bash
phpbb-test-board/feature_checks.py path/to/extension[@REF] [more extensions...]
```

Exercises the main feature of each phpbbmodders extension that has a check. One example: a moderator can't warn a user in a group that isn't ticked. `--help` lists the extensions with checks. To add one, write a `check_<name>()` function and add it to the `CHECKS` table, keyed by the extension's composer name.

### `screenshots.py`: documentation screenshots

```bash
phpbb-test-board/screenshots.py path/to/project[@REF] -o path/to/project/docs/images [--seed SCRIPT]
```

Saves PNG screenshots of an extension's or style's pages with [Playwright](https://playwright.dev/python/) and Chromium. The board is named "Example board" and uses English.
- **Extensions** are shown in prosilver, with their board and ACP pages.
- **Styles** are recognised by their `style.cfg`. The style is installed and made every user's style.

`--help` lists the projects with screenshots. To add one, write a `shots_<name>()` function and add it to the `SHOTS` table, keyed by the extension's composer name or the style's name from `style.cfg`.

**Options:**
- `--seed SCRIPT` runs a PHP script that fills the board with forums, topics and users first, as `php SCRIPT BOARD_ROOT`. Use it for styles and for any pages that need real content; a fresh board has one forum and one post. [seed-forum](https://github.com/phpbbmodders/seed-forum)'s `bin/seed-standard-fixtures.php` works well.
- `--build DIR` is for phpbbmodders/documentation only: the phpbbdocs-hugo build to serve.

For example, ProMinoDeux's screenshots:

```bash
phpbb-test-board/screenshots.py path/to/ProMinoDeux -o path/to/ProMinoDeux/docs/images \
  --seed path/to/seed-forum/bin/seed-standard-fixtures.php
```

### `template_a11y.py`: labels on icon-only links and buttons

```bash
phpbb-test-board/template_a11y.py path/to/project [more projects...]
```

Scans an extension's or style's templates (`styles/`, `adm/style/`, or a style's `template/`) for links and buttons that show only an icon. Each needs a `title` for the tooltip and screen-reader text, a `<span class="sr-only">` inside it or an `aria-label`, both from a language string:

```html
<a href="{U_CHECK}" title="{L_CHECK}"><i class="icon fa-search fa-fw" aria-hidden="true"></i><span class="sr-only">{L_CHECK}</span></a>
```

A `title` on the icon itself, phpBB's ACP pattern, counts as the tooltip, and a link around an image with alt text passes. Problems are printed as `FILE:LINE: message`, and the exit status is 1 if there are any. It needs no board and only Python's standard library.

## Extensions that need another extension

Pass the other extension's checkout with `--with`. Repeat it for several, in the order they must be enabled:

```bash
phpbb-test-board/smoke_test.py path/to/sfscompanion --with path/to/stopforumspam
```

- **Install order:** `--with` extensions are installed and enabled first and stay enabled. Only the extension under test goes through `smoke_test.py`'s disable and delete-data round trip.
- **In `feature_checks.py`:** a `--with` extension is installed only for the extensions whose `composer.json` requires it.
- **Missing packages:** if `composer.json` requires a package you didn't supply, the scripts print a note naming it.

## How a run works

1. **Clean start:** the board's database is restored from `board.clean.sqlite3`.
2. **Install:** the extension or style is copied from the given git ref, so uncommitted changes aren't tested.
3. **Serve:** PHP's built-in server serves the board on port 8083 (`--port`) with opcache off and PHP errors logged to `DIR/php-errors.log`.
4. **Restore:** afterwards, the extension or style is removed and the database restored again.

Files written outside the extension's own directory, for example under `store/`, are not reset automatically. `screenshots.py` removes the documentation build it copies there afterwards; `feature_checks.py` empties that build directory before its documentation check.
