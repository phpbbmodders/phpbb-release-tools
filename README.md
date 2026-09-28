# phpbb-release-tools

Tools for phpBB packages: two Bash scripts that build release zips (one for extensions, one for language packs), and a local test board for smoke-testing extensions (`phpbb-test-board/`).

## Tools

| Script | Builds | Zip name |
|---|---|---|
| [`git-release.sh`](git-release.sh) | A phpBB extension | `<vendor>-<name>-<version>.zip`, from `composer.json` |
| [`git-release-langpack.sh`](git-release-langpack.sh) | A phpBB language pack for the phpBB.com Customisation Database | `<languagename>_<version>.zip` |

Both scripts build the zip from the current git commit (`HEAD`), so uncommitted changes are not included. Files marked `export-ignore` in `.gitattributes` are left out. They were written and tested on Linux.

## Requirements

- `git-release.sh`: `git`, `jq`, `zip`
- `git-release-langpack.sh`: `git`, `curl`, `jq`, `unzip`, `zip`. For `--check` also `php`, `composer` and `sha256sum`.
- `phpbb-test-board/`: `git`, `php` (with the `sqlite3` extension), `composer`, `curl`, `jq`, `openssl`, Python 3 with `venv`, and ImageMagick's `convert` for the reassignthumbs feature check. The Python scripts create and use their own virtualenv (`phpbb-test-board/.venv`) on first run.

## Usage

### Extensions: `git-release.sh`

Run it inside the extension's git repository. `composer.json` must have a `name` (`vendor/name`) and a `version`.

```bash
git-release.sh
```

The zip is written to `../git-exported`, next to the repository. Its top folder is `vendor/name/`.

### Language packs: `git-release-langpack.sh`

Run it inside the language pack's git repository. With no arguments it prints help.

```bash
git-release-langpack.sh --check --release
```

- `--check` runs the [phpBB Translation Validator](https://github.com/phpbb/phpbb-translation-validator) (branch `1.6.x`, phpBB 3.3 only) on the exact zip contents. It uses the English files of the official phpBB release zip as the reference, checked against phpBB's published SHA-256.
- `--release` builds `<languagename>_<version>.zip` with a folder of the same name inside. Only `ext/`, `language/` and `styles/` are included, because the [Language Pack Validation Policy](https://area51.phpbb.com/docs/dev/3.3.x/language/validation.html) does not allow other files.
- With both options, the zip is written only if validation passes.
- The version defaults to the current stable phpBB 3.3 release from `https://version.phpbb.com/phpbb/versions.json`. Use `--version` to set it, for example `--version 3.3.16`.

Run `git-release-langpack.sh --help` for all options.

### Testing extensions: `phpbb-test-board/`

Builds a local phpBB board on SQLite and runs an extension on it with PHP error logging on, to catch problems that validation and syntax checks miss, such as pages that crash at runtime, forms that can never submit, or an extension that can't be enabled on a fresh board. For local testing only; never expose the board to a network.

```bash
phpbb-test-board/setup-board.sh -d ~/phpbb-test-board
export PHPBB_TEST_BOARD=~/phpbb-test-board
phpbb-test-board/smoke_test.py path/to/extension --ref origin/main
phpbb-test-board/feature_checks.py path/to/extension@origin/main
```

For an extension that needs another one, pass the other extension's checkout with `--with` (repeat it for several, in the order they must be enabled):

```bash
phpbb-test-board/smoke_test.py path/to/sfscompanion --with path/to/stopforumspam
```

- `setup-board.sh` installs the current phpBB 3.3 release (or `--version`) with no extensions, and keeps a clean copy of the database so every test starts from the same state. The admin password is generated and stored only in `board.env`.
- `smoke_test.py` installs the extension from git, loads board pages as a guest and as the admin, the extension's routes, ACP/MCP/UCP modules and cron tasks, then disables, deletes data and re-enables it. It reports any server error, empty page, phpBB debug notice or PHP error-log entry.
- `feature_checks.py` exercises the main feature of the phpbbmodders extensions listed in its `--help` (for example: a moderator can't warn a user in an unticked group). Add a check for a new extension in its `CHECKS` table.
- The board is restored after each run. Each script has `--help`.
- `--with` extensions are installed and enabled before the extension under test and stay enabled; only the extension under test goes through the disable and delete data round trip. `feature_checks.py` installs a `--with` extension only for the extensions whose `composer.json` requires it. If `composer.json` requires a package you didn't supply, the scripts print a note naming it.

## Tests

```bash
tests/test-release-scripts.sh
tests/test-phpbb-test-board.sh
```

The tests run in a temporary directory; the release script tests are offline, and the test-board tests only check options and errors (the first run creates the Python virtualenv). GitHub Actions runs them, and ShellCheck, on every pull request.

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/phpbbmodders/phpbb-release-tools/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/phpbbmodders/phpbb-release-tools/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- Code review, bug fixes, and documentation assisted by [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v2.0**.

See [LICENSE](LICENSE) for more information.
