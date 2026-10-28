# phpbb-release-tools

Build phpBB package ZIPs, publish GitHub Releases, wait for PR checks, and test extensions and styles on a local board. Written and tested on Linux.

## Tools

| Tool | Purpose |
|---|---|
| [`git-extensions.sh`](git-extensions.sh) | Package and release extensions |
| [`git-release-style.sh`](git-release-style.sh) | Package and release styles |
| [`git-release-langpack.sh`](git-release-langpack.sh) | Validate, package, and release language packs |
| [`pr-wait.py`](pr-wait.py) | Wait for a pull request's checks |
| [`phpbb-test-board/`](phpbb-test-board/) | Set up a local board, run checks, and take screenshots |

## Quick start

Clone the whole repository so the scripts can find their shared `lib/` helpers:

```bash
git clone https://github.com/phpbbmodders/phpbb-release-tools.git
cd phpbb-release-tools
export PATH="$PWD:$PWD/phpbb-test-board:$PATH"
```

The PATH change applies to the current shell. See [Installation](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Installation) for prerequisites and permanent setup.

Run a release script inside the package's Git repository:

```bash
git-extensions.sh --create                      # ZIP from local HEAD
git-extensions.sh --release --remote --dry-run  # ZIP and publication preview
git-extensions.sh --release --remote            # publish from GitHub's default branch
```

Use `git-release-style.sh` or `git-release-langpack.sh` for those package types. All three use local `HEAD` by default; `--remote` selects GitHub's default branch. Uncommitted changes are excluded. Run a tool with `--help` for its options.

## Documentation

The [Wiki](https://github.com/phpbbmodders/phpbb-release-tools/wiki) contains the full guides:

- [Release workflows](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Release-workflows), including package examples and common options.
- [Default-branch releases](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Default-branch-releases), including migration from `git-release-api.sh`.
- [GitHub releases and recovery](https://github.com/phpbbmodders/phpbb-release-tools/wiki/GitHub-releases-and-recovery), including rolling releases and `--force` replacement.
- [Waiting for PR checks](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Waiting-for-PR-checks).
- [Test-board setup](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Test-board-setup) and [maintainer checks](https://github.com/phpbbmodders/phpbb-release-tools/wiki/Maintainer-checks).
- [Forum update posts and screenshot galleries](docs/forum-update-posts.md).

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
