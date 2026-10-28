# phpbb-release-tools

Tools for phpBB packages: Bash scripts that build release ZIPs and publish them as GitHub Releases, and a local test board for testing extensions and styles and taking their documentation screenshots (`phpbb-test-board/`), and `pr-wait.py`, which waits for a pull request's checks to finish.

## Tools

| Script | Builds | Zip name |
|---|---|---|
| [`git-extensions.sh`](git-extensions.sh) | A phpBB extension | `<vendor>-<name>-<version>.zip`, from `composer.json` |
| [`git-release-style.sh`](git-release-style.sh) | A phpBB style | `<Stylename>_<version>.zip`, from `style.cfg` |
| [`git-release-langpack.sh`](git-release-langpack.sh) | A phpBB language pack for the phpBB.com Customisation Database | `<languagename>_<version>.zip` |

All three scripts build from the current Git commit (`HEAD`) by default. Add `--remote` to use the repository's default branch on GitHub (`main`, `master`, or its configured name). Uncommitted changes are not included. Files marked `export-ignore` in `.gitattributes` are left out. They were written and tested on Linux.

The scripts share source selection in [`lib/release-source.sh`](lib/release-source.sh) and GitHub publishing in [`lib/github-release.sh`](lib/github-release.sh). To put a script on your `PATH`, symlink it rather than copying it, so it can still find `lib/`.

See the [Wiki release workflows](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Release-workflows) for per-package examples and recovery guidance.

## Forum update posts

Prepare BBCode update drafts with a dated screenshot folder and its own
README gallery. See [BBCode update posts and screenshot galleries](docs/forum-update-posts.md)
for the required layout, image links, sizing, and preparation checks.

## Requirements

- `git-extensions.sh`: `git`, `jq`, `zip`
- `git-release-style.sh`: `git`, `zip`
- `git-release-langpack.sh`: `git`, `curl`, `jq`, `unzip`, `zip`. For `--check` also `php`, `composer` and `sha256sum`.
- `--remote` and `--release` also need an authenticated GitHub CLI (`gh`), `jq` and `timeout`. Publishing also uses GNU `stat` and needs permission to create releases and upload assets.
- `phpbb-test-board/`: see [its README](phpbb-test-board/README.md#requirements).
- `pr-wait.py`: Python 3.7 or later and an authenticated GitHub CLI (`gh`).

## Usage

The three scripts share the same command structure. Run them inside the package's Git repository, or use `--remote --repo OWNER/REPO` from elsewhere. With no arguments they print help.

```bash
git-extensions.sh --create                         # ZIP from local HEAD
git-extensions.sh --create --remote                # ZIP from GitHub's default branch
git-extensions.sh --release --remote --dry-run     # ZIP and release preview
git-extensions.sh --release --remote               # publish to GitHub Releases
git-extensions.sh --release --remote --force       # replace an existing tag/release ZIP
```

Use `git-release-style.sh` or `git-release-langpack.sh` for those package types.

| Option | Meaning |
|---|---|
| `-C`, `--create` | Build a ZIP only. |
| `-r`, `--release` | Build a ZIP and publish it to GitHub Releases. Local publication requires clean tracked files and an already-pushed `HEAD`. |
| `--remote` | Pin and package GitHub's default-branch commit instead of local `HEAD`. |
| `-n`, `--dry-run` | With `--release`, build the ZIP and preview generated notes without changing a GitHub tag, release, or asset. A same-named local ZIP can be replaced. |
| `-f`, `--force` | With `--release`, permit replacement of an existing version's tag, notes, and ZIP. GitHub-enforced immutability and tag rules still apply. |
| `-R`, `--repo OWNER/REPO` | Select the repository for `--remote`; otherwise infer it from the current checkout. Rejected without `--remote`. |
| `-o`, `--output DIR` | ZIP destination, defaulting to `../git-exported` relative to the repository root (or current directory when outside a checkout). Relative custom paths use that same root. |
| `-h`, `--help` | Show help. |

Existing `-g`/`--gh-release` remains an alias for `--release`, and `--output-dir` remains an alias for `--output`. Language packs now use `--release` for GitHub publication too; use `--create` for a ZIP only. Invalid usage exits with status 2.

### Extensions: `git-extensions.sh`

`composer.json` must have a `name` (`vendor/name`) and a `version`. The zip's top folder is `vendor/name/`.

### Styles: `git-release-style.sh`

`style.cfg` at the repository root must have a `name` and a `style_version`. The zip's top folder is the style name.

### Language packs: `git-release-langpack.sh`

```bash
git-release-langpack.sh --create --check
```

- `--check` (`-c`) runs the [phpBB Translation Validator](https://github.com/phpbb/phpbb-translation-validator) (branch `1.6.x`, phpBB 3.3 only) on the exact zip contents. It uses the English files of the official phpBB release zip as the reference, checked against phpBB's published SHA-256. It can run on its own, or with `--create` or `--release`; then nothing is written or published unless validation passes.
- The zip is `<languagename>_<version>.zip` with a folder of the same name inside. Only `ext/`, `language/` and `styles/` are included, because the [Language Pack Validation Policy](https://area51.phpbb.com/docs/dev/3.3.x/language/validation.html) does not allow other files.
- The version defaults to the current stable phpBB 3.3 release from `https://version.phpbb.com/phpbb/versions.json`. Use `--version` to set it, for example `--version 3.3.16`. With `--release` that phpBB version is the tag and release name.

Run `git-release-langpack.sh --help` for all options.

### Remote sources

`--remote` resolves GitHub's configured default branch and fetches its exact commit into an isolated temporary Git repository. Metadata, validation, and packaging all use that pinned commit. Your local branch and working files are preserved, including dirty or unpushed local changes. ZIP-only `--create --remote` does not inspect existing releases or generate release notes.

```bash
git-extensions.sh --release --remote --dry-run --repo phpbbmodders/phpbb-ext-wiki
git-release-style.sh --create --remote --repo OWNER/STYLE --output /tmp/style-packages
git-release-langpack.sh --create --remote --repo OWNER/LANGUAGE --check --version 3.3.17
```

The old `git-release-api.sh` entry point has been removed. Replace `git-release-api.sh --dry-run` with `git-extensions.sh --release --remote --dry-run`, and replace its `--release` operation with `git-extensions.sh --release --remote`. The `--repo` and `--output` options carry over. Update any personal aliases or automation still calling the old entry point.

### How GitHub releases behave

These rules apply to `--release` (including its `--gh-release` alias) in all three scripts.

The package version is the tag and release name, and release notes are generated by GitHub. Alpha, beta, RC, and dev versions are marked as prereleases. Archived repositories are rejected, since GitHub does not allow releases on them. For stable, alpha, beta, and RC versions an existing tag or release stops the operation unless `--force` is explicitly supplied.

A version ending in `-dev` is a rolling prerelease, including a build on the way to an alpha, beta or RC such as `1.1.0-a2-dev`. The first release creates it like any other release. Each later release moves the tag to the new commit, regenerates the release notes, and replaces the ZIP in the same GitHub release, so its URL stays the same. The new ZIP is uploaded under a temporary name and only replaces the old one after the upload is confirmed. The scripts print `Updated rolling release:` for this path and `Published release:` for a new release. A dry run shows what would change but never touches the tag, release, or asset. If the tag has to move, the dry-run notes still reflect where the tag points now.

`--force` updates an existing release in place, preserving its URL, and moves its tag to the selected commit. Stable versions remain stable; alpha/beta/RC versions remain prereleases. It also handles a tag without a release or a release whose tag is missing. ZIP replacement uses the same staging/upload verification as rolling releases, and unrelated assets are preserved. A forced dry run only previews those changes. The flag does not bypass GitHub's tag protections or immutable-release policy.

The tag is reserved at the pinned commit before the release is created. A failure after tag creation may leave that tag without a release. The release stays a draft until its asset upload is confirmed. If uploading or publishing fails, the script reports the release URL and exits with an error. Inspect the remote state before retrying. For stable, alpha, beta, and RC versions a retry stops when it finds the existing tag or release unless `--force` is supplied. For a `-dev` version a retry reuses the leftover tag or draft release and finishes the update. A publication timeout may mean publication succeeded but could not be confirmed. Requests have finite timeouts and remote write requests are not retried automatically.

### Waiting for a pull request's checks: `pr-wait.py`

Waits until every check on a pull request has finished, then exits with a status that says how they ended, so a person or an agent can start it in the background and act on the result. It follows new pushes: the checks it waits for are always those of the pull request's latest commit. When checks fail, it lists each one with its link.

```bash
./pr-wait.py https://github.com/phpbbmodders/contactadmin/pull/42
./pr-wait.py -R phpbbmodders/contactadmin 42 --timeout 30
```

| Exit status | Meaning |
|---|---|
| 0 | Every check passed (success, neutral or skipped) |
| 1 | At least one check failed, was cancelled or timed out |
| 2 | Bad arguments (with no arguments it prints help) |
| 3 | The pull request was closed or merged first |
| 4 | `--timeout` ran out first (default 60 minutes) |
| 5 | No check appeared on the latest commit within `--first-check-wait` (default 3 minutes) |
| 6 | `gh` is not installed, or failed five times in a row (not logged in, no such pull request, no network) |

It asks GitHub every `--interval` seconds (default 30). A result counts only once the same finished checks have been seen for `--settle` seconds (default 30), because GitHub can add a run a few seconds late, such as a pull request's own run after an earlier push run has already finished. Progress lines carry the time in UTC.

### Testing extensions and styles: `phpbb-test-board/`

Builds a local phpBB 3.3 board on SQLite and tests extensions on it: `smoke_test.py` checks that an extension doesn't break any page, `feature_checks.py` exercises its main feature, and `screenshots.py` takes documentation screenshots of extensions and styles, `rename_upgrade.py` checks that an extension moved to a new vendor name upgrades cleanly from the old one, and `template_a11y.py` checks that icon-only links and buttons have a tooltip and screen-reader text. For local testing only.

```bash
phpbb-test-board/setup-board.sh -d ~/phpbb-test-board
export PHPBB_TEST_BOARD=~/phpbb-test-board
phpbb-test-board/smoke_test.py path/to/extension --ref origin/main
```

See [`phpbb-test-board/README.md`](phpbb-test-board/README.md) for every script and option.

## Tests

```bash
tests/test-release-scripts.sh
bash tests/test-release-api.sh
tests/test-phpbb-test-board.sh
tests/test-pr-wait.sh
```

The tests run in a temporary directory; the release script tests are offline, the test-board tests only check options and errors (the first run creates the Python virtualenv), and the `pr-wait.py` tests use a fake `gh`. GitHub Actions runs them, and ShellCheck, on every pull request.

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/phpbbmodders/phpbb-release-tools/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/phpbbmodders/phpbb-release-tools/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- Code review, bug fixes, and documentation assisted by [Claude](https://www.anthropic.com/claude).
- Release-script consolidation, regression tests, and documentation assisted by [Codex](https://openai.com/codex/).

## License

This project is licensed under the **GNU General Public License v2.0**.

See [LICENSE](LICENSE) for more information.
