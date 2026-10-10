"""Git facts the tracker reads: branch, HEAD, remote, uncommitted files. The tracker never changes git."""

from __future__ import annotations

import re
import subprocess
from functools import cache
from pathlib import Path


def run_git(cwd: str | Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def git(cwd: str | Path, *args: str) -> str:
    r = run_git(cwd, *args)
    return r.stdout.strip() if r and r.returncode == 0 else ""


def contains(cwd: str | Path, branch: str) -> bool | None:
    """HEAD holds the tip of `branch` (the local branch, else origin's copy); None when this clone has neither, or
    git cannot tell."""
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        tip = git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if tip:
            r = run_git(cwd, "merge-base", "--is-ancestor", tip, "HEAD")
            return {0: True, 1: False}.get(r.returncode) if r else None
    return None


@cache  # a worktree's branch and remote do not change during one command
def worktree(cwd: str | Path) -> tuple[Path | None, str]:
    """The worktree root that holds `cwd`, and its branch ("" when detached), read from git's files rather than by
    running git: a linked worktree's or a submodule's `.git` is a file that names its git directory."""
    here = Path(cwd).resolve()
    root = next((d for d in (here, *here.parents) if (d / ".git").exists()), None)
    if not root:
        return None, ""
    dot = root / ".git"
    try:
        gitdir = dot if dot.is_dir() else root / dot.read_text().partition("gitdir:")[2].strip()
        head = (gitdir / "HEAD").read_text().strip()
    except OSError:
        return root, ""
    return root, head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else ""


def branch_of(cwd: str | Path) -> str:
    return worktree(cwd)[1]


def head_of(cwd: str | Path) -> str:
    return git(cwd, "rev-parse", "HEAD")


@cache
def remote_of(cwd: str | Path) -> str:
    return git(cwd, "config", "--get", "remote.origin.url")


STATUS_FIELDS = {"1": 8, "u": 10, "?": 1}  # `git status --porcelain=v2`: the fields before an entry's path


def changed_files(cwd: str | Path, since: float) -> list[str]:
    """The files that differ from HEAD or are new (not ignored) and were written after `since`: the work not yet
    committed since then, whichever tool, agent or session wrote it. A deleted file has no time and does not count.
    `--no-optional-locks`: git takes no index lock, so a command that runs at the same time does not fail."""
    root = worktree(cwd)[0]
    if not root or not since:
        return []
    out = []
    status = git(root, "--no-optional-locks", "status", "--porcelain=v2", "-z", "--no-renames", "--untracked-files=all")
    for entry in status.split("\0"):
        n = STATUS_FIELDS.get(entry[:1])
        path = entry.split(" ", n)[-1] if n else ""
        try:
            if path and (root / path).stat().st_mtime > since:
                out.append(path)
        except OSError:
            pass
    return out


def worktree_key(cwd: str | Path, branch: str) -> str:
    """Names one worktree on one branch, for a `tracker use` choice."""
    return f"{worktree(cwd)[0] or Path(cwd).resolve()}@{branch}"


def default_branches(cwd: str | Path) -> set[str]:
    head = git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return {"main", "master", "develop", "trunk"} | ({head.split("/", 1)[-1]} if head else set())


def repo_slug(remote: str) -> str:
    """The repo's path on its host from a git remote URL (scp-like ssh, ssh:// or https), lower-cased: `owner/name`,
    or a nested group's whole path (`group/sub/name`). "" for a local path."""
    m = re.match(r"(?:[a-z+]+://)?(?:[^@/]+@)?[^/:]+(?::\d+)?[:/](.+?)(?:\.git)?/?$", remote.strip(), re.I)
    return m[1].lower() if m else ""


def cwd_repo(cwd: str | Path) -> str:
    """`owner/name` of the repo that holds `cwd`, from its remote; "" without one."""
    return repo_slug(remote_of(cwd))
