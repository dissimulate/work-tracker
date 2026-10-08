#!/usr/bin/env python3
"""Releases the plugin from main: the CHANGELOG.md section, the manifest version, a commit, a tag, the push and a
GitHub release. It stops at the first check that fails, before it writes anything.

    scripts/release.py patch|minor|major|X.Y.Z [--dry-run] [--yes]

The section is the lines under `## Unreleased` in CHANGELOG.md, grouped by `### <Kind>`, Kind one of KINDS. That
file may have uncommitted edits: the release commit takes them.

--dry-run prints the section and changes nothing; it only warns about uncommitted files. Without --yes the script asks
before it releases, and with no terminal to ask on it stops.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = "CHANGELOG.md"
MANIFEST = ".claude-plugin/plugin.json"
KINDS = ("Added", "Changed", "Fixed", "Removed")
UNRELEASED = "## Unreleased"


class Stop(Exception):
    """A check failed: the message says what to do."""


def git(*args: str, cwd: Path = ROOT) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if done.returncode:
        raise Stop(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.rstrip("\n")  # keep a leading space: `status --porcelain` starts lines with one


def next_version(current: str, bump: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", bump):
        if tuple(map(int, bump.split("."))) <= tuple(map(int, current.split("."))):
            raise Stop(f"{bump} is not after {current}")
        return bump
    major, minor, patch = map(int, current.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise Stop(f"bump {bump!r}: give patch, minor, major or X.Y.Z")


def split_unreleased(text: str) -> tuple[str, list[tuple[str, str]], str]:
    """CHANGELOG.md as (text up to and with the Unreleased heading, its entries, the released sections)."""
    start = text.find(f"\n{UNRELEASED}\n")
    if start < 0:
        raise Stop(f"{CHANGELOG} has no '{UNRELEASED}' heading")
    head_end = start + len(UNRELEASED) + 2
    rest = text.find("\n## ", head_end)
    body, after = (text[head_end:], "") if rest < 0 else (text[head_end:rest + 1], text[rest + 1:])
    entries, kind = [], None
    for line in body.splitlines():
        if line.startswith("### "):
            kind = line[4:].strip()
            if kind not in KINDS:
                raise Stop(f"{CHANGELOG} Unreleased: heading {line!r} is not one of {', '.join(KINDS)}")
        elif line.startswith("- "):
            if kind is None:
                raise Stop(f"{CHANGELOG} Unreleased: {line!r} is under no '### <Kind>' heading")
            entries.append([kind, line[2:]])
        elif line.strip():
            if not entries:
                raise Stop(f"{CHANGELOG} Unreleased: {line!r} is not a '- ' line")
            entries[-1][1] += " " + line.strip()
    return text[:head_end], [(k, t) for k, t in entries], after


def notes(entries: list[tuple[str, str]]) -> str:
    """The entries as Markdown, grouped by kind in KINDS order."""
    groups = [(kind, [t for k, t in entries if k == kind]) for kind in KINDS]
    return "\n\n".join(f"### {kind}\n\n" + "\n".join(f"- {t}" for t in texts) for kind, texts in groups if texts)


def released(text: str, version: str, date: str, section: str) -> str:
    """CHANGELOG.md with an empty Unreleased section and the new release's section under it."""
    head, _, after = split_unreleased(text)
    return f"{head}\n## {version} - {date}\n\n{section}\n" + (f"\n{after}" if after else "")


def manifest_version(text: str) -> str:
    return json.loads(text)["version"]


def run_gates() -> None:
    for gate in ([sys.executable, "-m", "unittest", "discover", "tests"], ["uvx", "ruff", "check"]):
        print(f"$ {' '.join(gate)}", flush=True)
        if subprocess.run(gate, cwd=ROOT).returncode:
            raise Stop(f"{' '.join(gate)} failed: fix it, then run the release again")


def release(bump: str, dry_run: bool, yes: bool) -> None:
    if git("branch", "--show-current") != "main":
        raise Stop("release from main")
    dirty = [line[3:] for line in git("status", "--porcelain").splitlines() if line[3:] != CHANGELOG]
    if dirty and not dry_run:
        raise Stop(f"commit or stash these first: {', '.join(dirty)}")
    git("fetch", "-q", "origin", "main")
    if subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=ROOT).returncode:
        raise Stop("main is behind origin/main: pull first")
    last = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    manifest = (ROOT / MANIFEST).read_text()
    current = manifest_version(manifest)
    if last != f"v{current}":
        raise Stop(f"{MANIFEST} has {current}, the last tag is {last}: they must match")
    version = next_version(current, bump)
    tag = f"v{version}"
    if git("tag", "--list", tag):
        raise Stop(f"tag {tag} exists")

    changelog = (ROOT / CHANGELOG).read_text()
    _, entries, _ = split_unreleased(changelog)
    if not entries:
        raise Stop(f"nothing to release: no line under '{UNRELEASED}' in {CHANGELOG}")
    section = notes(entries)
    print(f"{last} -> {tag}\n\n{section}\n")
    if dry_run:
        if dirty:
            print(f"Uncommitted, so a release stops: {', '.join(dirty)}")
        return
    if not yes:
        if not sys.stdin.isatty():
            raise Stop("no terminal to ask on: run again with --yes")
        if input(f"Release {tag}? [y/N] ").strip().lower() != "y":
            raise Stop("not released")

    run_gates()
    date = datetime.date.today().isoformat()
    (ROOT / CHANGELOG).write_text(released(changelog, version, date, section))
    (ROOT / MANIFEST).write_text(manifest.replace(f'"version": "{current}"', f'"version": "{version}"', 1))
    if manifest_version((ROOT / MANIFEST).read_text()) != version:
        raise Stop(f"could not set the version in {MANIFEST}: set it by hand and commit")
    git("add", CHANGELOG, MANIFEST)
    git("commit", "-q", "-m", f"work-tracker {version}", "-m", section)
    git("tag", "-a", tag, "-m", f"work-tracker {version}")
    sha = git("rev-parse", "HEAD")
    print(f"Committed and tagged {tag} at {sha}")
    try:
        git("push", "--atomic", "origin", "main", tag)
    except Stop as e:
        raise Stop(f"{e}\nThe commit and tag are local. Fix the cause, then: git push --atomic origin main {tag}")
    print(f"Pushed main and {tag}")
    if not shutil.which("gh"):
        print(f"No gh: make the GitHub release by hand, from the {version} section of {CHANGELOG}")
    elif subprocess.run(["gh", "release", "create", tag, "--title", f"work-tracker {version}", "--notes", section],
                        cwd=ROOT).returncode:
        print(f"The GitHub release failed: run gh release create {tag} with the {version} section as --notes")
    print(f"A marketplace that pins this plugin by sha gets {version} only when its pin moves to {sha}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release the plugin from main.")
    parser.add_argument("bump", help="patch, minor, major or X.Y.Z")
    parser.add_argument("--dry-run", action="store_true", help="print the release notes, change nothing")
    parser.add_argument("--yes", action="store_true", help="release without asking")
    args = parser.parse_args(argv)
    try:
        release(args.bump, args.dry_run, args.yes)
    except Stop as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
