# phpbb-release-tools

Two Bash scripts that build release zips for phpBB packages: one for phpBB extensions, one for phpBB language packs.

## Tools

| Script | Builds | Zip name |
|---|---|---|
| [`git-release.sh`](git-release.sh) | A phpBB extension | `<vendor>-<name>-<version>.zip`, from `composer.json` |
| [`git-release-langpack.sh`](git-release-langpack.sh) | A phpBB language pack for the phpBB.com Customisation Database | `<languagename>_<version>.zip` |

Both scripts build the zip from the current git commit (`HEAD`), so uncommitted changes are not included. Files marked `export-ignore` in `.gitattributes` are left out. They were written and tested on Linux.

## Requirements

- `git-release.sh`: `git`, `jq`, `zip`
- `git-release-langpack.sh`: `git`, `curl`, `jq`, `unzip`, `zip`. For `--check` also `php`, `composer` and `sha256sum`.

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

## Tests

```bash
tests/test-release-scripts.sh
```

The tests run offline in a temporary directory. GitHub Actions runs them, and ShellCheck, on every pull request.

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
