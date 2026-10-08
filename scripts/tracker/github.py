"""PR state from GitHub (`gh`): `sync` writes it into tickets; `match_pr` finds a branch's ticket from its PR."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .markdown import format_value
from .model import (PR_MATCH_TTL_S, SYNC_MIN_INTERVAL_S, append_log, branch_entry, locked, pr_key, put_entry,
    today, unblocked, Record, Tracker)
from .git import default_branches, remote_of, repo_slug
from .session import match_cwd, named_in, Match

PR_FIELDS = "number,headRefName,state,isDraft,mergedAt,baseRefName,updatedAt,isCrossRepository"
GH_LIST_LIMIT = 300  # newest PRs per repo in one call
GH_LOOKUPS_MAX = 5  # single-PR calls per sync, for tickets whose PR is older than that list
GH_TIMEOUT_S = 8  # one call
_deadline: float | None = None  # no gh call runs past this (time.monotonic()); None: no limit


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


def gh_reviews(prs: set[tuple[str, int]]) -> dict[str, dict] | None:
    """The review facts of open PRs, by `pr_key`, from one call; None when gh fails. A PR GitHub does not find is left
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


def review_facts(pr: dict) -> dict:
    """What a move needs from one PR: the review decision, who is asked and who asked for changes or approved (bots
    left out), when the head was committed and when changes were last asked for, the checks and merge state, and
    the unresolved threads. Empty facts are left out."""
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
        "decision": (pr.get("reviewDecision") or "").lower(),
        "requested": sorted(filter(None, (x.get("login") or x.get("name") for x in asked))),
        "changes": by("CHANGES_REQUESTED"),
        "approved": by("APPROVED"),
        "reviewed": max((when(r.get("submittedAt")) for r in reviews if r.get("state") == "CHANGES_REQUESTED"),
                        default=0),
        "pushed": when(head.get("committedDate")),
        "checks": ((head.get("statusCheckRollup") or {}).get("state") or "").lower(),
        "merge": (pr.get("mergeStateStatus") or "").lower(),
        "threads": sum(not t.get("isResolved") for t in nodes("reviewThreads")),
        "draft": bool(pr.get("isDraft")),
    }
    return {k: v for k, v in facts.items() if v}


PR_EVENT = {"merged": "merged", "open": "open for review", "draft": "open as a draft", "closed": "closed unmerged"}


def sync(tr: Tracker, force: bool, min_interval: float = SYNC_MIN_INTERVAL_S) -> list[str]:
    """Pull PR facts from GitHub into ticket frontmatter (the sync-owned keys only). Returns change lines.
    GitHub is asked first, without the lock; the tracker is then reloaded and written under it."""
    if not tr.repos:
        return []
    if not force and time.time() - tr.state().get("last_sync", 0) < min_interval:
        return []
    # The open PRs the tickets know already: their review facts are asked for with the lists.
    known = {(tr.repo_of(t), int(t.get("pr"))) for t in tr.tickets
             if t.get("pr_state") in ("open", "draft") and str(t.get("pr")).isdigit() and tr.repo_of(t)}
    with ThreadPoolExecutor(len(tr.repos) + 1) as pool:  # at once: a sync costs the time of its slowest call
        asked = pool.submit(gh_reviews, known)
        prs_by_repo = dict(zip(tr.repos, pool.map(gh_prs, tr.repos)))
        reviews = asked.result()
    failed = [repo for repo, prs in prs_by_repo.items() if prs is None]
    if failed:
        return [f"sync skipped: gh failed or timed out for {', '.join(failed)}"]
    found, lookups = {}, 0
    for t in tr.tickets:
        repo = tr.repo_of(t)
        # Only started work has a PR: a todo ticket on a branch that holds a PR is not in it yet. A merged PR is
        # final: a later PR from a branch the ticket shared is other tickets' work.
        started = t.get("status") in ("in-progress", "done")
        if not started or t.get("pr_state") == "merged" or not repo or not (t.get("branch") or t.get("pr")):
            continue
        prs = prs_by_repo.get(repo, [])
        pr = pick_pr(prs, t)
        # The list holds only the newest PRs; ask for an older one on its own.
        if not pr and len(prs) >= GH_LIST_LIMIT and lookups < GH_LOOKUPS_MAX:
            lookups += 1
            pr = gh_pr_of(repo, t)
        if pr:
            found[t.id] = (repo, pr)
    opened = {(repo, int(pr["number"])) for repo, pr in found.values() if pr["state"] == "OPEN"}
    if reviews is not None:
        if opened - known:  # opened since the last sync
            reviews |= gh_reviews(opened - known) or {}
        reviews = {k: v for k, v in reviews.items() if k in {pr_key(repo, n) for repo, n in opened}}
    with locked():
        return apply_sync(Tracker(tr.root), found, reviews)


def apply_sync(tr: Tracker, found: dict[str, tuple[str, dict]], reviews: dict[str, dict] | None = None) -> list[str]:
    """Write what `sync` read: each ticket's PR keys (and a log line per PR event), and the open PRs' review facts
    (None: the review call failed, so the last ones stay)."""
    changes, events = [], {}
    blocked = {t.id for t in tr.tickets if tr.blockers(t)}
    for t in tr.tickets:
        if t.id not in found:
            continue
        repo, pr = found[t.id]
        pr_state = "merged" if pr["state"] == "MERGED" else "closed" if pr["state"] == "CLOSED" else \
            "draft" if pr["isDraft"] else "open"
        upd = {"pr": str(pr["number"]), "pr_state": pr_state, "base": pr["baseRefName"]}
        if pr_state == "merged":
            upd["merged_at"] = pr["mergedAt"][:19] + "Z"  # GitHub gives UTC to the second
        # A base that is another ticket's branch makes this ticket wait on it (Tracker.stacked_on).
        diff = {k: v for k, v in upd.items() if str(t.get(k)) != str(v)}
        if diff:
            diff["updated"] = today()
            t.save(diff)
            changes.append(f"{t.id}: " + ", ".join(f"{k}={format_value(v)}" for k, v in diff.items() if k != "updated"))
            if "pr_state" in diff:
                pr_name = f"{repo}#{upd['pr']}" if len(tr.repos) > 1 else f"#{upd['pr']}"
                events.setdefault((pr_name, pr_state), []).append(t.id)
    for (pr_name, pr_state), ids in events.items():
        append_log(tr, f"PR {pr_name} {PR_EVENT[pr_state]}", ids)
    changes += unblocked(tr, blocked)
    state = tr.state()
    state["last_sync"] = time.time()
    if reviews is not None:
        state["reviews"] = reviews
    tr.save_state(state)
    return changes


def match_pr(m: Match, cwd: str | Path) -> tuple[Match, str]:
    """A branch on no ticket: ask GitHub for its PR and find the ticket the PR names (its ticket or Issue id), in the
    title, else in the body. One ticket: record the branch on it and match it. Several: a line that names them, for
    the user to choose. Asked at most once per PR_MATCH_TTL_S per branch; only at `start` and session start."""
    tr = m.tracker
    repo = repo_slug(remote_of(cwd))
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
                t.save({"branch": m.branch, "updated": today()})
                append_log(tr, f"{t.id} is built on branch {m.branch} (named in the {where} of PR #{pr['number']})",
                           [t.id])
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
