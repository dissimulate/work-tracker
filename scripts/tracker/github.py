"""The GitHub adapter: PR state from GitHub (`gh`), in the tracker's words. `sync` reads each ticket's PR and the open
PRs' PR_FACTS, mapping GitHub's words onto the tracker's, and `model.apply_prs` writes them; `match_pr` finds a branch's
ticket from its PR. Only this module knows GitHub's words; another forge's adapter would give the same."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .model import (FORMS, OPEN_PR, PR_MATCH_TTL_S, SYNC_MIN_INTERVAL_S, apply_prs, branch_entry, linked_prs, locked, pr_key,
    put_entry, set_branch, utc_seconds, Record, Tracker)
from .git import cwd_repo, default_branches
from .session import match_cwd, named_in, Match

NAME = "GitHub"  # the forge, where a text names it; the model and the views say "PR" only
BRANCH_PRS_MAX = 10  # the newest PRs sync asks for per branch, forks' too: a branch name repeats across forks
GH_TIMEOUT_S = 8  # one call
_deadline: float | None = None  # no gh call runs past this (time.monotonic()); None: no limit
# A `gh` command that changes a PR: the hooks sync after it (hooks.PR_TRIGGER).
PR_COMMAND = re.compile(r"\bgh\s+pr\s+(create|merge|ready|close|reopen|edit)\b")


def budget(seconds: float | None) -> None:
    """Give this process's gh calls `seconds` in all (None: no limit). A hook must end before Claude Code's timeout
    stops it; a call the budget cuts short fails like any other, and the next sync tries again."""
    global _deadline
    _deadline = None if seconds is None else time.monotonic() + seconds


def gh(*args: str, fields: str | None = None, partial: bool = False):
    """`gh <args>`, with `--json <fields>` when fields are given, parsed; None when gh is missing, fails, times out, or
    the budget is spent. `partial`: a failed call's output still counts when it parses (GraphQL answers what it can
    and names the errors)."""
    timeout = GH_TIMEOUT_S if _deadline is None else min(GH_TIMEOUT_S, _deadline - time.monotonic())
    if timeout < 1:
        return None
    try:
        r = subprocess.run(["gh", *args, *(["--json", fields] if fields else [])], capture_output=True, text=True,
                           timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 or partial else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None




def pr_url(repo: str, number: object) -> str:
    """A PR's page, or "" when no repo says where it is."""
    return f"https://github.com/{repo}/pull/{number}" if repo else ""


def named_prs(t: Record, repo: str) -> set[str]:
    """The numbers of the PRs a ticket names in this repo: the ones `sync` follows, and its `PR:` links."""
    linked = (x.number for x in linked_prs(t) if x.repo.lower() == repo.lower())
    return {n for n in (*t.prs, *linked) if n.isdigit()}


def ticket_prs(prs: list[dict], t: Record, repo: str) -> list[dict]:
    """A ticket's PRs: those it names, and those from its branch. The closed ones only while there are no others."""
    named, own = named_prs(t, repo), own_prs(prs)
    mine = [p for p in prs if str(p["number"]) in named or p in own and p["headRefName"] == t.get("branch")]
    return [p for p in mine if pr_state(p) != "closed"] or mine


def own_prs(prs: list[dict]) -> list[dict]:
    """The PRs from the repo's own branches. Anyone can open a PR from a fork, with any branch name, so a branch name
    matches a ticket only in the repo itself (where the tracker's work lives: `in_repos`)."""
    return [p for p in prs if not p.get("isCrossRepository")]


# What sync reads of each PR: its own state, base and head (pr_of, ticket_prs), and what a ticket's move needs
# (review_facts, for model.whose_move).
PR_FRAGMENT = """fragment F on PullRequest {
  number headRefName state isDraft mergedAt baseRefName isCrossRepository reviewDecision mergeStateStatus
  reviewRequests(first: 20) { nodes { requestedReviewer {
    ... on User { login } ... on Team { name } ... on Bot { login } ... on Mannequin { login } } } }
  latestReviews(first: 50) { nodes { author { __typename login } state submittedAt } }
  reviewThreads(first: 100) { nodes { isResolved } }
  commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
}"""


def gh_prs(tr: Tracker, tickets: list[Record]) -> dict[str, list[dict] | None] | None:
    """The PRs the tickets can be on, per repo, from one GraphQL call: those a ticket names by their numbers, and while
    it has no merged PR the newest BRANCH_PRS_MAX from its branch, each as PR_FRAGMENT reads it. None for a repo GitHub does not answer for, and in
    place of all when gh fails. A PR GitHub does not find is left out."""
    asked: dict[str, tuple[set[str], set[str]]] = {}  # repo: (PR numbers, branches)
    for t in tickets:
        numbers, branches = asked.setdefault(tr.repo_of(t), (set(), set()))
        numbers |= named_prs(t, tr.repo_of(t))
        if t.get("branch") and t.pr_state != "merged":
            branches.add(str(t.get("branch")))
    repos = sorted(asked)
    query = []
    for i, repo in enumerate(repos):
        owner, _, name = repo.partition("/")
        numbers, branches = asked[repo]
        fields = [f"p{n}: pullRequest(number: {n}) {{ ...F }}" for n in sorted(numbers, key=int)]
        fields += [f"b{j}: pullRequests(headRefName: {json.dumps(b)}, first: {BRANCH_PRS_MAX}, orderBy: {{field: "
                   "CREATED_AT, direction: DESC}) { nodes { ...F } }" for j, b in enumerate(sorted(branches))]
        query.append(f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {' '.join(fields)} }}")
    got = gh("api", "graphql", "-f", f"query=query {{ {' '.join(query)} }} {PR_FRAGMENT}", partial=True)
    data = got.get("data") if isinstance(got, dict) else None
    if not isinstance(data, dict):
        return None
    out: dict[str, list[dict] | None] = {}
    for i, repo in enumerate(repos):
        found = data.get(f"r{i}")
        if not isinstance(found, dict):
            out[repo] = None
            continue
        prs = [x for alias, v in found.items() if v for x in (v.get("nodes") or [] if alias.startswith("b") else [v])]
        out[repo] = list({p["number"]: p for p in prs if p}.values())
    return out


# GitHub's words for a PR's review decision, checks rollup and merge state, as the tracker's (PR_FACTS). Any other word
# says nothing that stops the work: no review decided, no checks, a PR that can merge.
REVIEW = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes"}
CHECKS = {"FAILURE": "failing", "ERROR": "failing", "PENDING": "running", "EXPECTED": "running", "SUCCESS": "passing"}
MERGE = {"DIRTY": "conflict", "BEHIND": "behind", "BLOCKED": "blocked"}


def review_facts(pr: dict) -> dict:
    """One PR's PR_FACTS. A bot's review counts for none (`changes`, `approved`, `reviewed`); empty facts are left
    out."""
    def nodes(key: str) -> list[dict]:
        return [x for x in ((pr.get(key) or {}).get("nodes") or []) if x]

    def when(ts: str | None) -> int:
        return (utc_seconds(FORMS["time"].read("", ts)) or 0) if ts else 0

    reviews = [r for r in nodes("latestReviews")
               if (r.get("author") or {}).get("login") and r["author"].get("__typename") != "Bot"]

    def by(state: str) -> list[str]:
        return sorted(r["author"]["login"] for r in reviews if r.get("state") == state)

    asked = [x.get("requestedReviewer") or {} for x in nodes("reviewRequests")]
    head = (nodes("commits") or [{}])[0].get("commit") or {}
    facts = {
        "review": REVIEW.get(pr.get("reviewDecision") or ""),
        "requested": sorted(filter(None, (x.get("login") or x.get("name") for x in asked))),
        "changes": by("CHANGES_REQUESTED"),
        "approved": by("APPROVED"),
        "reviewed": max((when(r.get("submittedAt")) for r in reviews if r.get("state") == "CHANGES_REQUESTED"),
                        default=0),
        "pushed": when(head.get("committedDate")),
        "checks": CHECKS.get((head.get("statusCheckRollup") or {}).get("state") or ""),
        "merge": MERGE.get(pr.get("mergeStateStatus") or ""),
        "threads": sum(not t.get("isResolved") for t in nodes("reviewThreads")),
        "draft": bool(pr.get("isDraft")),
    }
    return {k: v for k, v in facts.items() if v}


def pr_state(pr: dict) -> str:
    """A PR's state as gh gives it (PR_FRAGMENT), in the tracker's words."""
    return ("merged" if pr["state"] == "MERGED" else "closed" if pr["state"] == "CLOSED"
            else "draft" if pr["isDraft"] else "open")


def pr_of(pr: dict) -> dict:
    """A PR as gh gives it (PR_FRAGMENT), in the tracker's words (model.apply_prs)."""
    return {"number": pr["number"], "state": pr_state(pr),
            "merged_at": FORMS["time"].read("merged_at", pr["mergedAt"]) if pr.get("mergedAt") else ""}


SKIPPED = "sync skipped"  # the start of the one change line of a sync that read nothing


def skipped(changes: list[str]) -> bool:
    """A sync's change lines say it read nothing: gh failed (`sync`), or the sync raised (its caller's line)."""
    return bool(changes) and changes[0].startswith(SKIPPED)


def sync(tr: Tracker, force: bool, min_interval: float = SYNC_MIN_INTERVAL_S) -> list[str]:
    """Pull PR state from GitHub: each ticket's PRs into its frontmatter (`prs`, `merged_at`), the
    open PRs' PR_FACTS into .state.json `prs` (`model.apply_prs`). Returns change lines. GitHub is asked first, in
    one call (`gh_prs`) and without the lock; the tracker is then reloaded and written under it."""
    if not tr.repos:
        return []
    if not force and time.time() - tr.state().get("last_sync", 0) < min_interval:
        return []
    # Only started work has a PR: a todo ticket on a branch that holds a PR is not in it yet. A merged ticket is final,
    # but for a PR newly linked to it: a later PR from a branch the ticket shared is other tickets' work.
    def to_read(t: Record) -> bool:
        repo = tr.repo_of(t)
        return (t.started and repo in tr.repos and (t.get("branch") or named_prs(t, repo))
                and (t.pr_state != "merged" or bool(named_prs(t, repo) - set(t.prs))))

    tickets = [t for t in tr.tickets if to_read(t)]
    prs_by_repo = gh_prs(tr, tickets) if tickets else {}
    failed = (sorted({tr.repo_of(t) for t in tickets}) if prs_by_repo is None
              else [repo for repo, prs in prs_by_repo.items() if prs is None])
    if failed:
        return [f"{SKIPPED}: gh failed or timed out for {', '.join(failed)}"]
    found, facts = {}, {}
    for t in tickets:
        repo = tr.repo_of(t)
        prs = ticket_prs(prs_by_repo.get(repo, []), t, repo)
        if prs:
            found[t.id] = (repo, [pr_of(p) for p in prs])
            facts.update({pr_key(repo, p["number"]): {**review_facts(p), "base": p["baseRefName"]}
                          for p in prs if pr_state(p) in OPEN_PR})
    with locked():
        return apply_prs(Tracker(tr.root), found, facts)


def match_pr(m: Match, cwd: str | Path) -> tuple[Match, str]:
    """A branch on no ticket: ask GitHub for its PR and find the ticket the PR names (its ticket or Issue id), in the
    title, else in the body. One ticket: record the branch on it and match it. Several: a line that names them, for
    the user to choose. Asked at most once per PR_MATCH_TTL_S per branch; only at `start` and session start."""
    tr = m.tracker
    repo = cwd_repo(cwd)
    if m.tickets or not m.branch or m.branch in default_branches(cwd) or repo not in (r.lower() for r in tr.repos):
        return m, ""
    state = tr.state()
    asked = branch_entry(state.get("pr_match", {}), repo, m.branch)
    if asked and time.time() - asked.get("at", 0) < PR_MATCH_TTL_S:
        return m, asked.get("note", "")
    prs = gh("pr", "list", "--repo", repo, "--head", m.branch, "--state", "all",
             fields="number,title,body,isCrossRepository")
    if prs is None:
        return m, ""  # gh missing or failed: ask again next time
    note, hits, where = "", [], ""
    pr = max(own_prs(prs), key=lambda x: x["number"], default=None)
    if pr:
        for where in ("title", "body"):
            hits = [t for t in tr.tickets if named_in(pr.get(where) or "", t)]
            if hits:
                break
    with locked():
        if len(hits) == 1:
            t = Tracker(tr.root).lookup(hits[0].id)
            if t and not t.get("branch"):
                set_branch(tr, t, m.branch, f"named in the {where} of PR #{pr['number']}")
                return match_cwd(cwd, tracker=Tracker(tr.root)), ""
            note = (f"Branch {m.branch} is on no ticket. Its PR #{pr['number']} names {hits[0].id}, which is "
                    f"built on branch {hits[0].get('branch')}; if this branch is for it too, `tracker use "
                    f"{hits[0].id}`.")
        elif hits:
            note = (f"Branch {m.branch} is on no ticket. The {where} of its PR #{pr['number']} names "
                    f"{', '.join(t.id for t in hits)}: ask the user which one this branch is for, then `tracker use "
                    f"<id>`.")
        state = tr.state()
        put_entry(state.setdefault("pr_match", {}), repo, m.branch, {"at": int(time.time()), "note": note})
        tr.save_state(state)
    return m, note
