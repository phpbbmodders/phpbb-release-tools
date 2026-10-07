# phpbb-release-tools

Tools for phpBB packages: Bash scripts that build release ZIPs and publish them as GitHub Releases, and a local test board for testing extensions and styles and taking their documentation screenshots (`phpbb-test-board/`).

## Tools

| Script | Builds | Zip name |
|---|---|---|
| [`git-extensions.sh`](git-extensions.sh) | A phpBB extension | `<vendor>-<name>-<version>.zip`, from `composer.json` |
| [`git-release-style.sh`](git-release-style.sh) | A phpBB style | `<Stylename>_<version>.zip`, from `style.cfg` |
| [`git-release-langpack.sh`](git-release-langpack.sh) | A phpBB language pack for the phpBB.com Customisation Database | `<languagename>_<version>.zip` |
| [`git-release-api.sh`](git-release-api.sh) | A phpBB extension and a GitHub Release from the remote default branch | `<vendor>-<name>-<version>.zip`, from committed `composer.json` |

`git-extensions.sh`, `git-release-style.sh` and `git-release-langpack.sh` build from the current git commit (`HEAD`); `git-release-api.sh` builds from the repository's default branch on GitHub (`main`, `master`, or whatever it is set to). Uncommitted changes are not included. Files marked `export-ignore` in `.gitattributes` are left out. They were written and tested on Linux.

The scripts share their GitHub release code in [`lib/github-release.sh`](lib/github-release.sh). To put a script on your `PATH`, symlink it rather than copying it, so it can still find `lib/`.

## Requirements

- `git-extensions.sh`: `git`, `jq`, `zip`
- `git-release-style.sh`: `git`, `zip`
- `git-release-langpack.sh`: `git`, `curl`, `jq`, `unzip`, `zip`. For `--check` also `php`, `composer` and `sha256sum`.
- `git-release-api.sh`: `git`, `jq`, `zip`, and GNU coreutils (`timeout`, `mktemp`, `stat`).
- `--gh-release` and `git-release-api.sh` also need an authenticated GitHub CLI (`gh`), `jq` and `timeout`, with permission to create releases and upload assets.
- `phpbb-test-board/`: see [its README](phpbb-test-board/README.md#requirements).

## Usage

`git-extensions.sh`, `git-release-style.sh` and `git-release-langpack.sh` share the same modes. Run them inside the package's git repository. With no arguments they print help.

```bash
git-extensions.sh --create                  # build the ZIP in ../git-exported
git-extensions.sh --gh-release --dry-run    # build it and preview the GitHub release
git-extensions.sh --gh-release              # build it and publish a GitHub release
```

| Option | Meaning |
|---|---|
| `-C`, `--create` | Build the ZIP in `../git-exported`, next to the repository. |
| `-g`, `--gh-release` | Build the ZIP and publish it as a GitHub Release from `HEAD`. Tracked files must be committed and `HEAD` pushed. |
| `-n`, `--dry-run` | With `--gh-release`: preview the release and its generated notes without changing anything on GitHub. |
| `-h`, `--help` | Show help. |

Invalid usage exits with status 2.

### Extensions: `git-extensions.sh`

`composer.json` must have a `name` (`vendor/name`) and a `version`. The zip's top folder is `vendor/name/`.

### Styles: `git-release-style.sh`

`style.cfg` at the repository root must have a `name` and a `style_version`. The zip's top folder is the style name.

### Language packs: `git-release-langpack.sh`

```bash
git-release-langpack.sh --create --check
```

- `--check` (`-c`) runs the [phpBB Translation Validator](https://github.com/phpbb/phpbb-translation-validator) (branch `1.6.x`, phpBB 3.3 only) on the exact zip contents. It uses the English files of the official phpBB release zip as the reference, checked against phpBB's published SHA-256. It can run on its own, or with `--create` or `--gh-release`; then nothing is written or published unless validation passes.
- The zip is `<languagename>_<version>.zip` with a folder of the same name inside. Only `ext/`, `language/` and `styles/` are included, because the [Language Pack Validation Policy](https://area51.phpbb.com/docs/dev/3.3.x/language/validation.html) does not allow other files.
- The version defaults to the current stable phpBB 3.3 release from `https://version.phpbb.com/phpbb/versions.json`. Use `--version` to set it, for example `--version 3.3.16`. With `--gh-release` that phpBB version is the tag and release name.
- `--release` was renamed to `--create`.

Run `git-release-langpack.sh --help` for all options.

### GitHub releases from the default branch: `git-release-api.sh`

Uses the GitHub API to resolve the latest commit on the repository's default branch, generate release notes, create a draft release, upload the package, and publish the release. An isolated Git fetch supplies that exact commit for `git archive`, preserving the extension folder layout and exclusions. The local checkout is not changed, so nothing needs to be checked out or pushed locally.

Run from the extension repository, or supply `--repo OWNER/REPO`:

```bash
git-release-api.sh --dry-run --repo phpbbmodders/phpbb-ext-wiki
git-release-api.sh --release --repo phpbbmodders/phpbb-ext-wiki
```

`--dry-run` builds the local ZIP and previews GitHub-generated release notes without creating a tag, release, or asset. `--release` publishes it. With no arguments the script prints help. Use `--output DIR` to change the ZIP destination from `../git-exported`. Packaging replaces a same-named local ZIP only after the archive is complete.

### How GitHub releases behave

These rules apply to `--gh-release` and to `git-release-api.sh`.

The package version is the tag and release name, and release notes are generated by GitHub. Alpha, beta, RC, and dev versions are marked as prereleases. Archived repositories are rejected, since GitHub does not allow releases on them. For stable, alpha, beta, and RC versions an existing tag or release stops the operation; those releases and assets are never replaced.

A version ending in `-dev` is a rolling prerelease. The first release creates it like any other release. Each later release moves the tag to the new commit, regenerates the release notes, and replaces the ZIP in the same GitHub release, so its URL stays the same. The new ZIP is uploaded under a temporary name and only replaces the old one after the upload is confirmed. The scripts print `Updated rolling release:` for this path and `Published release:` for a new release. A dry run shows what would change but never touches the tag, release, or asset. If the tag has to move, the dry-run notes still reflect where the tag points now.

The tag is reserved at the pinned commit before the release is created. A failure after tag creation may leave that tag without a release. The release stays a draft until its asset upload is confirmed. If uploading or publishing fails, the script reports the release URL and exits with an error. Inspect the remote state before retrying. For stable, alpha, beta, and RC versions a retry stops when it finds the existing tag or release. For a `-dev` version a retry reuses the leftover tag or draft release and finishes the update. A publication timeout may mean publication succeeded but could not be confirmed. Requests have finite timeouts and remote write requests are not retried automatically.

### Testing extensions and styles: `phpbb-test-board/`

Builds a local phpBB 3.3 board on SQLite and tests extensions on it: `smoke_test.py` checks that an extension doesn't break any page, `feature_checks.py` exercises its main feature, and `screenshots.py` takes documentation screenshots of extensions and styles. For local testing only.

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
