"""The GitHub adapter: PR state from GitHub (`gh`), in the tracker's words. `sync` reads each ticket's PR and the open
PRs' PR_FACTS, mapping GitHub's words onto the tracker's, and `model.apply_prs` writes them; `match_pr` finds a branch's
ticket from its PR. Only this module knows GitHub's words; another forge's adapter would give the same."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .model import (OPEN_PR, PR_MATCH_TTL_S, SYNC_MIN_INTERVAL_S, apply_prs, branch_entry, locked, pr_key, put_entry,
    set_branch, Record, Tracker)
from .git import cwd_repo, default_branches
from .session import match_cwd, named_in, Match

PR_FIELDS = "number,headRefName,state,isDraft,mergedAt,baseRefName,updatedAt,isCrossRepository"
GH_LIST_LIMIT = 300  # newest PRs per repo in one call
GH_LOOKUPS_MAX = 5  # single-PR calls per sync, for tickets whose PR is older than that list
GH_TIMEOUT_S = 8  # one call
_deadline: float | None = None  # no gh call runs past this (time.monotonic()); None: no limit
# A `gh` command that changes a PR: the hooks sync after it (hooks.PR_TRIGGER).
PR_COMMAND = re.compile(r"\bgh\s+pr\s+(create|merge|ready|close|reopen|edit)\b")


def budget(seconds: float | None) -> None:
    """Give this process's gh calls `seconds` in all (None: no limit). A hook must end before Claude Code's timeout
    stops it; a call the budget cuts short fails like any other, and the next sync tries again."""
    global _deadline
    _deadline = None if seconds is None else time.monotonic() + seconds


def gh(*args: str, fields: str | None = PR_FIELDS, partial: bool = False):
    """`gh <args> --json <fields>` (no `--json` when fields is None), parsed; None when gh is missing, fails, times
    out, or the budget is spent. `partial`: a failed call's output still counts when it parses (GraphQL answers what
    it can and names the errors)."""
    timeout = GH_TIMEOUT_S if _deadline is None else min(GH_TIMEOUT_S, _deadline - time.monotonic())
    if timeout < 1:
        return None
    try:
        r = subprocess.run(["gh", *args, *(["--json", fields] if fields else [])], capture_output=True, text=True,
                           timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 or partial else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def gh_prs(repo: str) -> list[dict] | None:
    return gh("pr", "list", "--repo", repo, "--state", "all", "--limit", str(GH_LIST_LIMIT))


def gh_pr_of(repo: str, ticket: Record) -> dict | None:
    """One ticket's PR, asked for on its own: by number, or by the ticket's branch."""
    if ticket.get("pr"):
        return gh("pr", "view", str(ticket.get("pr")), "--repo", repo)
    prs = gh("pr", "list", "--repo", repo, "--state", "all", "--head", str(ticket.get("branch")))
    return pick_pr(prs, ticket) if prs else None


def pick_pr(prs: list[dict], ticket: Record) -> dict | None:
    """The ticket's PR: by its number, else the PR from its branch. A fork's PR from a branch of the same name is
    someone else's work (`own_prs`)."""
    if ticket.get("pr"):
        return next((p for p in prs if str(p["number"]) == str(ticket.get("pr"))), None)
    mine = [p for p in own_prs(prs) if p["headRefName"] == ticket.get("branch")]
    rank = {"OPEN": 0, "MERGED": 1, "CLOSED": 2}
    return min(mine, key=lambda p: (rank.get(p["state"], 3), -p["number"]), default=None)


def own_prs(prs: list[dict]) -> list[dict]:
    """The PRs from the repo's own branches. Anyone can open a PR from a fork, with any branch name, so a branch name
    matches a ticket only in the repo itself (where the tracker's work lives: `in_repos`)."""
    return [p for p in prs if not p.get("isCrossRepository")]


# What a ticket's move needs from each open PR (model.whose_move), for all of them in one GraphQL call.
REVIEW_FIELDS = """fragment F on PullRequest {
  isDraft reviewDecision mergeStateStatus
  reviewRequests(first: 20) { nodes { requestedReviewer {
    ... on User { login } ... on Team { name } ... on Bot { login } ... on Mannequin { login } } } }
  latestReviews(first: 50) { nodes { author { __typename login } state submittedAt } }
  reviewThreads(first: 100) { nodes { isResolved } }
  commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
}"""


def gh_facts(prs: set[tuple[str, int]]) -> dict[str, dict] | None:
    """The PR_FACTS of open PRs, by `pr_key`, from one call; None when gh fails. A PR GitHub does not find is left
    out."""
    if not prs:
        return {}
    repos = sorted({repo for repo, _ in prs})
    query = []
    for i, repo in enumerate(repos):
        owner, _, name = repo.partition("/")
        pulls = " ".join(f"p{n}: pullRequest(number: {n}) {{ ...F }}" for r, n in sorted(prs) if r == repo)
        query.append(f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {pulls} }}")
    got = gh("api", "graphql", "-f", f"query=query {{ {' '.join(query)} }} {REVIEW_FIELDS}", fields=None, partial=True)
    data = got.get("data") if isinstance(got, dict) else None
    if not isinstance(data, dict):
        return None
    out = {}
    for repo, n in prs:
        pr = (data.get(f"r{repos.index(repo)}") or {}).get(f"p{n}")
        if pr:
            out[pr_key(repo, n)] = review_facts(pr)
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
        return int(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) if ts else 0

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


def pr_of(pr: dict) -> dict:
    """A PR as gh gives it (PR_FIELDS), in the tracker's words (model.apply_prs)."""
    state = "merged" if pr["state"] == "MERGED" else "closed" if pr["state"] == "CLOSED" else \
        "draft" if pr["isDraft"] else "open"
    return {"number": pr["number"], "state": state, "base": pr["baseRefName"],
            "merged_at": pr["mergedAt"][:19] + "Z" if pr.get("mergedAt") else ""}  # GitHub gives UTC to the second


def sync(tr: Tracker, force: bool, min_interval: float = SYNC_MIN_INTERVAL_S) -> list[str]:
    """Pull PR state from GitHub: each ticket's PR into its frontmatter (`pr`, `pr_state`, `base`, `merged_at`), the
    open PRs' PR_FACTS into .state.json `prs` (`model.apply_prs`). Returns change lines. GitHub is asked first,
    without the lock; the tracker is then reloaded and written under it."""
    if not tr.repos:
        return []
    if not force and time.time() - tr.state().get("last_sync", 0) < min_interval:
        return []
    # The open PRs the tickets know already: their review facts are asked for with the lists.
    known = {(tr.repo_of(t), int(t.get("pr"))) for t in tr.tickets
             if t.get("pr_state") in OPEN_PR and str(t.get("pr")).isdigit() and tr.repo_of(t)}
    with ThreadPoolExecutor(len(tr.repos) + 1) as pool:  # at once: the lists and the known PRs' facts
        asked = pool.submit(gh_facts, known)
        prs_by_repo = dict(zip(tr.repos, pool.map(gh_prs, tr.repos)))
        facts = asked.result()
    failed = [repo for repo, prs in prs_by_repo.items() if prs is None]
    if failed:
        return [f"sync skipped: gh failed or timed out for {', '.join(failed)}"]
    found, lookups = {}, 0
    for t in tr.tickets:
        repo = tr.repo_of(t)
        # Only started work has a PR: a todo ticket on a branch that holds a PR is not in it yet. A merged PR is
        # final: a later PR from a branch the ticket shared is other tickets' work.
        if not t.started or t.get("pr_state") == "merged" or not repo or not (t.get("branch") or t.get("pr")):
            continue
        prs = prs_by_repo.get(repo, [])
        pr = pick_pr(prs, t)
        # The list holds only the newest PRs; ask for an older one on its own.
        if not pr and len(prs) >= GH_LIST_LIMIT and lookups < GH_LOOKUPS_MAX:
            lookups += 1
            pr = gh_pr_of(repo, t)
        if pr:
            found[t.id] = (repo, pr_of(pr))
    opened = {(repo, int(pr["number"])) for repo, pr in found.values() if pr["state"] in OPEN_PR}
    if facts is not None:
        if opened - known:  # opened since the last sync
            facts |= gh_facts(opened - known) or {}
        facts = {k: v for k, v in facts.items() if k in {pr_key(repo, n) for repo, n in opened}}
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
