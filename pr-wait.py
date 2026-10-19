#!/usr/bin/env python3
"""
Wait for a GitHub pull request's checks to finish, then exit with a status
that says how they ended.

Polls the pull request with the GitHub CLI (`gh pr view`) every --interval
seconds. The checks followed are always those of the pull request's latest
commit, so a new push while waiting restarts the wait on the new commit.
The result counts only once the same finished checks have been seen for
--settle seconds: GitHub can take a few seconds to add a run (for example a
pull request's own run after an earlier push run already finished).
Prints one line whenever the progress changes, and a summary at the end that
lists every failed check with its link. Meant for a person or an agent that
starts it in the background and acts on the exit status.

Exit status:
  0  every check passed (success, neutral or skipped)
  1  at least one check failed (failure, cancelled, timed out, ...)
  2  bad arguments
  3  the pull request was closed or merged before the checks finished
  4  --timeout ran out first
  5  no check appeared on the latest commit within --first-check-wait
  6  gh is not installed, or failed five times in a row (not logged in, no
     such pull request, no network)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_CLOSED = 3
EXIT_TIMEOUT = 4
EXIT_NO_CHECKS = 5
EXIT_GH_ERROR = 6

# gh failures in a row before giving up; single ones are usually the network
GH_MAX_FAILURES = 5
GH_TIMEOUT_SECONDS = 60

# Check-run conclusions and commit-status states that count as passed
PASSED = {'SUCCESS', 'NEUTRAL', 'SKIPPED'}
# Commit-status states that are still running
STATUS_PENDING = {'PENDING', 'EXPECTED'}


def parse_args(argv: list) -> argparse.Namespace:
    """Read the command line; with no arguments print help and exit 2."""
    parser = argparse.ArgumentParser(
        description='Wait for a pull request\'s checks to finish, following new pushes.',
        epilog='Exit status: 0 passed, 1 failed, 2 bad arguments, 3 closed or merged, '
               '4 timed out, 5 no checks appeared, 6 gh failed.')
    parser.add_argument('pr', help='pull request number, branch or URL, as gh pr view takes it')
    parser.add_argument('-R', '--repo', help='OWNER/REPO, when PR is not a URL and the current '
                                             'directory is not a clone of the repository')
    parser.add_argument('-t', '--timeout', type=float, default=60,
                        help='minutes to wait in all before giving up (default: 60)')
    parser.add_argument('-i', '--interval', type=float, default=30,
                        help='seconds between checks of the pull request (default: 30)')
    parser.add_argument('-w', '--first-check-wait', type=float, default=3,
                        help='minutes to wait for the first check to appear on a commit '
                             'before deciding the repository runs none (default: 3)')
    parser.add_argument('-s', '--settle', type=float, default=30,
                        help='seconds the same finished checks must stay unchanged before the '
                             'result counts, so a run GitHub adds late is not missed (default: 30)')
    if not argv:
        parser.print_help(sys.stderr)
        sys.exit(2)
    args = parser.parse_args(argv)
    for name in ('timeout', 'interval', 'first_check_wait', 'settle'):
        if not getattr(args, name) > 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than 0")
    return args


class GhMissing(RuntimeError):
    """gh is not installed, so retrying cannot help."""


def fetch_pr(pr: str, repo: str | None) -> dict:
    """Return the pull request's state, latest commit and checks from gh; raise RuntimeError."""
    command = ['gh', 'pr', 'view', pr, '--json', 'state,url,headRefOid,statusCheckRollup']
    if repo:
        command += ['--repo', repo]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=GH_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as error:
        raise GhMissing('gh (the GitHub CLI) is not installed') from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'gh did not answer within {GH_TIMEOUT_SECONDS} seconds') from error
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'gh exited with {result.returncode}')
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError('gh returned something that is not JSON') from error
    if not isinstance(data, dict) or not data.get('headRefOid') or 'state' not in data:
        raise RuntimeError('gh returned no pull request state or commit')
    return data


def classify(check: dict) -> tuple:
    """Return (name, 'pending' | 'passed' | 'failed', result, link) for one rollup entry."""
    if check.get('__typename') == 'StatusContext':
        # A commit status, set by an outside service
        name = check.get('context') or '?'
        state = (check.get('state') or '').upper()
        link = check.get('targetUrl') or ''
        if state in STATUS_PENDING or not state:
            return name, 'pending', state or 'PENDING', link
        return name, 'passed' if state in PASSED else 'failed', state, link
    # A check run, from GitHub Actions or a GitHub App
    name = check.get('name') or '?'
    if check.get('workflowName'):
        name = f"{check['workflowName']} / {name}"
    link = check.get('detailsUrl') or ''
    if (check.get('status') or '').upper() != 'COMPLETED':
        return name, 'pending', (check.get('status') or 'QUEUED').upper(), link
    conclusion = (check.get('conclusion') or '').upper()
    return name, 'passed' if conclusion in PASSED else 'failed', conclusion or 'UNKNOWN', link


def say(message: str) -> None:
    """Print a progress line with the time, flushed so a log shows it at once."""
    print(f"{time.strftime('%I:%M:%S %p UTC', time.gmtime())} {message}", flush=True)


def wait(args: argparse.Namespace) -> int:
    """Poll until the checks finish, the pull request closes, or time runs out."""
    start = time.monotonic()
    deadline = start + args.timeout * 60
    head = None
    head_seen_at = start
    last_progress = None
    # The finished checks last seen, and since when, for --settle
    finished = None
    finished_since = start
    gh_failures = 0
    waiting_for = 'an answer from gh'
    while True:
        try:
            data = fetch_pr(args.pr, args.repo)
            gh_failures = 0
        except GhMissing as error:
            say(str(error))
            return EXIT_GH_ERROR
        except RuntimeError as error:
            gh_failures += 1
            say(f'gh failed ({gh_failures}/{GH_MAX_FAILURES}): {error}')
            if gh_failures >= GH_MAX_FAILURES:
                return EXIT_GH_ERROR
            data = None

        if data is not None:
            url = data.get('url') or args.pr
            if data['headRefOid'] != head:
                if head is not None:
                    say(f"New commit {data['headRefOid'][:7]} pushed; waiting on its checks")
                head = data['headRefOid']
                head_seen_at = time.monotonic()
                last_progress = None
                finished = None

            checks = [classify(check) for check in data.get('statusCheckRollup') or []]
            pending = [c for c in checks if c[1] == 'pending']
            failed = [c for c in checks if c[1] == 'failed']

            if data['state'] != 'OPEN':
                say(f"{url} is {data['state'].lower()}; stopped waiting")
                return EXIT_CLOSED

            waiting_for = 'the first check'
            if pending:
                waiting_for = f'{len(pending)} running checks'
            elif checks:
                waiting_for = 'the finished checks to settle'
            progress = (len(checks), len(pending), len(failed))
            if checks and progress != last_progress:
                say(f'{head[:7]}: {len(checks) - len(pending)} of {len(checks)} checks done, '
                    f'{len(failed)} failed')
                last_progress = progress

            settled = False
            if not checks or pending:
                finished = None
            else:
                outcome = sorted((name, result) for name, _, result, _ in checks)
                if outcome != finished:
                    finished = outcome
                    finished_since = time.monotonic()
                settled = time.monotonic() - finished_since >= args.settle

            if checks and not pending and settled:
                if failed:
                    say(f'{url}: {len(failed)} of {len(checks)} checks failed on {head[:7]}')
                    for name, _, result, link in failed:
                        print(f'  {result.lower()}: {name}' + (f'  {link}' if link else ''))
                    return EXIT_FAILED
                say(f'{url}: all {len(checks)} checks passed on {head[:7]}')
                return EXIT_PASSED

            if not checks and time.monotonic() - head_seen_at >= args.first_check_wait * 60:
                say(f'{url}: no checks appeared on {head[:7]} within '
                    f'{args.first_check_wait:g} minutes')
                return EXIT_NO_CHECKS

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            say(f'Gave up after {args.timeout:g} minutes, still waiting for {waiting_for}')
            return EXIT_TIMEOUT
        pause = min(args.interval, remaining)
        if finished is not None:
            # Look again as soon as the finished checks have settled
            pause = min(pause, max(args.settle - (time.monotonic() - finished_since), 0.01))
        time.sleep(pause)


def main() -> int:
    """Run the wait; Ctrl+C stops it with the usual status 130."""
    args = parse_args(sys.argv[1:])
    try:
        return wait(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
