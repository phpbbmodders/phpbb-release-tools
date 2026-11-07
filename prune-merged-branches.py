#!/usr/bin/env python3
"""
Delete local git branches whose work is already merged on GitHub.

Works on one checkout, or on every checkout directly inside a folder. In each
one, a local branch other than the default branch, and not checked out in any
worktree, is deleted when:
  - it is an ancestor of origin's default branch (a normal merge):
    `git branch -d`;
  - or a pull request from it was merged on GitHub with exactly this branch's
    tip as its head commit (a squash or rebase merge, which `git branch -d`
    refuses): `git branch -D`. Because the tip must match, a branch that got
    commits after its PR was merged is kept.
Every other branch is kept and listed with the reason. Checkouts whose origin
isn't on GitHub, and wiki clones, are skipped.

Inputs:  a checkout, or a folder of checkouts; needs git and an authenticated gh
Outputs: one line per deleted or kept branch, then a summary
Exit status: 0 on success, 1 when a git or gh call failed for a checkout,
             2 for bad arguments.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    """Run git in repo and return its stdout; raise CalledProcessError on failure."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def github_slug(repo: Path) -> str:
    """Return OWNER/NAME from origin's URL, or '' when origin isn't a GitHub repository."""
    try:
        # The configured URL, not `git remote get-url`, which applies url.insteadOf rewrites.
        url = git(repo, "config", "--get", "remote.origin.url").strip()
    except subprocess.CalledProcessError:
        return ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not m or m.group(1).endswith(".wiki"):
        return ""
    return m.group(1)


def checkouts(path: Path) -> list:
    """Return path itself when it is a checkout, else the checkouts directly inside it."""
    if (path / ".git").exists():
        return [path]
    return sorted(p for p in path.iterdir() if (p / ".git").is_dir())


def prune(repo: Path, slug: str, dry_run: bool) -> tuple:
    """Delete one checkout's merged branches; return (deleted, kept, failures)."""
    deleted = kept = failures = 0
    git(repo, "fetch", "-q", "--prune", "origin")
    default = subprocess.run(["gh", "api", f"repos/{slug}", "-q", ".default_branch"],
                             capture_output=True, text=True, check=True).stdout.strip()
    checked_out = set(re.findall(r"^branch refs/heads/(.+)$", git(repo, "worktree", "list", "--porcelain"), re.M))
    refs = git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
    for line in filter(None, refs):
        branch, sha = line.split(" ")
        if branch == default or branch in checked_out:
            continue
        ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha,
                                   f"origin/{default}"]).returncode == 0
        how = f"merged into {default}" if ancestor else ""
        if not ancestor:
            prs = subprocess.run(["gh", "pr", "list", "-R", slug, "--head", branch, "--state", "merged",
                                  "--json", "number,headRefOid"], capture_output=True, text=True)
            if prs.returncode != 0:
                print(f"{repo.name} {branch}: kept (could not ask GitHub)")
                kept += 1
                continue
            match = [p for p in json.loads(prs.stdout or "[]") if p["headRefOid"] == sha]
            if match:
                how = f"PR #{match[0]['number']} merged from this exact commit"
        if not how:
            print(f"{repo.name} {branch}: kept (not merged, or has commits after its merge)")
            kept += 1
            continue
        if not dry_run:
            try:
                git(repo, "branch", "-d" if ancestor else "-D", branch)
            except subprocess.CalledProcessError as e:
                print(f"{repo.name} {branch}: FAILED to delete: {(e.stderr or '').strip()[:200]}")
                failures += 1
                continue
        print(f"{repo.name} {branch}: {'would delete' if dry_run else 'deleted'} ({how}, {sha[:7]})")
        deleted += 1
    return deleted, kept, failures


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Delete local branches already merged on GitHub (squash merges included, "
                    "only when the merged PR's head is the branch's exact tip).",
        epilog="Exit status: 0 success, 1 a git or gh call failed, 2 bad arguments.")
    ap.add_argument("path", nargs="?", type=Path, help="a git checkout, or a folder whose direct subfolders are checkouts")
    ap.add_argument("-n", "--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    args = ap.parse_args()
    if args.path is None:
        ap.print_help()
        return 2
    if not args.path.is_dir():
        ap.error(f"{args.path} is not a folder")

    totals = [0, 0, 0]
    for repo in checkouts(args.path.resolve()):
        slug = github_slug(repo)
        if not slug:
            continue
        try:
            counts = prune(repo, slug, args.dry_run)
        except subprocess.CalledProcessError as e:
            print(f"{repo.name}: FAILED: {(e.stderr or '').strip()[:200]}")
            counts = (0, 0, 1)
        totals = [a + b for a, b in zip(totals, counts)]
    print(f"== {'would delete' if args.dry_run else 'deleted'} {totals[0]}, kept {totals[1]}, failures {totals[2]}")
    return 1 if totals[2] else 0


if __name__ == "__main__":
    sys.exit(main())
