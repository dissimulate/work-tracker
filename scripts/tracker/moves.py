"""Whose move a ticket under way waits on: this side (the user and the AI) or the people or thing named. Computed from
the review facts `sync` reads and the ticket's blockers (MOVE_RULE); nothing writes it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .markdown import Link
from .schema import COORDINATOR
from .model import link_title, short, Record, Tracker

# What a move reads of an open PR, in the tracker's own words: a forge's adapter (github.py) maps its own words onto
# these, so the model and the views know no forge. Where `sync` keeps them: STATE_RULES `prs`.
PR_FACTS = {
    "review": "the review decision: approved, or changes (requested); none while no review decides",
    "requested": "who is asked to review",
    "changes": "who requested changes",
    "approved": "who approved",
    "reviewed": "when changes were last requested (epoch s)",
    "pushed": "when the PR's head was committed (epoch s)",
    "checks": "failing, running or passing",
    "merge": "what stops the merge: conflict, behind (its base) or blocked (by the repo's rules); none when it can",
    "threads": "how many review threads are unresolved",
    "draft": "the PR is a draft",
    "base": "the branch the PR would merge into; a base that is other tickets' branch makes the ticket wait on them "
            "(computed; depends_on does not change)",
}
YOU = "you"
MOVE_RULE = (
    "A ticket under way has a move: whose turn it waits on, computed, never written. In order: merge conflicts, "
    "failing checks or requested changes not re-requested are yours; a requested review is the reviewer's; an open "
    "decision it waits on is its owner's: a human by name, the session on a ticket, or the coordinator, and a "
    "human's when no owner is named and no coordinator is watching; an open action yours; an external blocker is "
    "that blocker's; then unresolved review threads, an approval (CI's while its checks run), a draft or a PR with "
    "no review requested are yours. With several open PRs, the most urgent of their moves, yours first, naming that "
    "PR. `next` holds your own next action: do not write a wait on a reviewer into it.")


@dataclass
class Move:
    who: str  # YOU (also for an open action), the reviewers, "CI", an open decision's (decision_move) or a blocker's id
    what: str = ""  # why it is their move, as a state: "review requested", "checks failing"
    rank: int = 0  # its rule's place in MOVE_RULE: the more urgent first
    quiet: bool = False  # your own work going on as planned, where nothing has stopped to wait for you
    human: bool = False  # the move is a human's to make: an open action, or a decision a human must answer

    @property
    def mine(self) -> bool:
        return self.who == YOU

    def text(self, sep: str = ": ") -> str:
        return self.who + (sep + self.what if self.what else "")


# ---------------------------------------------------------------- submissions and reviews
# `tracker submit` hands a ticket's plan or built code (SUBMIT_WHAT) to its reviewer, `tracker review` answers it: both
# are events on the ticket, and `handoffs` reads where each stands from them.

REVIEW_OF = {"plan": "plan review", "code": "plan conformance check"}  # each SUBMIT_WHAT's review, by its name
READY = {"plan": "plan ready for review", "code": "built code ready for the plan conformance check"}


@dataclass
class Handoff:
    """Where one ticket's plan or built code stands in the log."""
    submission: dict | None = None  # the latest submission that no later review answered
    review: dict | None = None  # the latest review
    fixed: set = field(default_factory=set)  # the numbers of that review's findings marked fixed since


def handoffs(events: list[dict]) -> dict[tuple[str, str], Handoff]:
    """Per (ticket, what) the log holds a submission, review or fix of, where it stands, in one pass over the events:
    a review answers the submission before it, and a later submission replaces an earlier one."""
    out: dict[tuple[str, str], Handoff] = {}
    for e in events:
        if e["kind"] not in ("submission", "review", "finding.fixed") or not e["refs"]:
            continue
        h = out.setdefault((e["refs"][0], str(e.get("what"))), Handoff())
        if e["kind"] == "submission":
            h.submission = e
        elif e["kind"] == "review":
            h.submission, h.review, h.fixed = None, e, set()
        elif h.review and e.get("of") == h.review["seq"]:
            h.fixed |= set(e.get("findings") or [])
    return out


def finding_numbers(review: dict) -> list[int]:
    """The numbers of a review event's findings."""
    return [f["n"] for f in review.get("findings") or [] if isinstance(f, dict) and isinstance(f.get("n"), int)]


def last_submission(events: list[dict], ticket: str, what: str) -> dict | None:
    """The ticket's latest submission of `what` that no later review of `what` answered: the one a review answers."""
    h = handoffs(events).get((ticket, what))
    return h.submission if h else None


def last_review(events: list[dict], ticket: str, what: str) -> dict | None:
    """The ticket's latest review of `what`: the one `--fixed` marks findings of."""
    h = handoffs(events).get((ticket, what))
    return h.review if h else None


def owner_kind(tr: Tracker, d: Record) -> tuple[str, str]:
    """Who a decision's owner names, and its name: ("coordinator", "") also for none; ("ticket", its id) for the
    session on a ticket under way; ("human", the name) for a person; ("human", "") for any other record of the
    tracker, such as a ticket no session works on, which names no person."""
    owner = str(d.get("owner") or "").strip()
    if not owner or owner.lower() == COORDINATOR:
        return "coordinator", ""
    rec = tr.lookup(owner)
    if rec and rec.kind == "ticket" and rec.in_flight:
        return "ticket", rec.id
    return "human", "" if rec else owner


def decision_move(tr: Tracker, d: Record, coordinated: bool) -> Move:
    """An open decision's move (MOVE_RULE): its owner's, the coordinator's while one watches the tracker
    (`session.coordinated`), else a human's."""
    (kind, name), what = owner_kind(tr, d), f"{d.id} open"
    if kind == "ticket":
        return Move(f"{name}'s session", what, 5)
    if kind == "coordinator" and coordinated:
        return Move(COORDINATOR, what, 5)
    return Move(name or "human", what, 5, human=True)


def whose_move(tr: Tracker, t: Record, coordinated: bool = False) -> Move | None:
    """The move of a ticket under way (MOVE_RULE); None for any other ticket, and while `sync` has read the review facts
    of none of its open PRs. `coordinated`: a coordinator watches the tracker (`session.move_of` asks)."""
    if not t.in_flight:
        return None
    open_prs = tr.open_prs(t)
    if not open_prs:
        return pr_move(tr, t, {}, coordinated)
    moves = [(n, pr_move(tr, t, r, coordinated)) for n, r in open_prs if r is not None]
    if not moves:
        return None
    number, move = min(moves, key=lambda x: (not x[1].mine, x[1].rank))
    return replace(move, what=f"#{number} {move.what}".strip()) if len(open_prs) > 1 else move


PR_PAGE = re.compile(r"https?://[^\s)/]+/([^/\s)]+/[^/\s)]+)/pull/(\d+)")  # groups: owner/name, the number


@dataclass
class LinkedPr:
    url: str
    repo: str
    number: str
    text: str


def linked_prs(t: Record) -> list[LinkedPr]:
    """The PRs a ticket's `PR:` links point at."""
    pages = ((PR_PAGE.search(x.text), x.text) for x in t.links if x.label == "PR")
    return [LinkedPr(m[0], m[1], m[2], text) for m, text in pages if m]


def is_pr_link(link: Link) -> bool:
    return link.label == "PR" and bool(PR_PAGE.search(link.text))


def unread_link_move(tr: Tracker, t: Record) -> Move | None:
    """The move of a ticket whose `PR:` links `sync` has not read: nothing is known of those PRs, but they exist. A link
    to a repo `sync` does not read says so, as a sync would not change it."""
    repo = tr.repo_of(t).lower()
    unread = [x for x in linked_prs(t) if x.repo.lower() != repo or x.number not in t.prs]
    if not unread:
        return None
    here = any(x.repo.lower() == repo for x in unread)
    return Move(YOU, "PR not synced" if here else f"PR in {unread[0].repo}", 10, quiet=True)


def pr_move(tr: Tracker, t: Record, r: dict, coordinated: bool) -> Move:
    """A ticket's move as one open PR's facts make it; empty facts, for a ticket with no open PR."""
    is_open = bool(tr.open_prs(t))
    threads = r.get("threads", 0)
    open_threads = f"{threads} unresolved review thread{'s' * (threads != 1)}"
    requested = r.get("requested", [])
    approved = "approved" + (f" by {', '.join(r['approved'])}" if r.get("approved") else "") \
        if r.get("review") == "approved" else ""
    if r.get("merge") == "conflict":
        return Move(YOU, "merge conflicts", 1)
    if r.get("checks") == "failing":
        return Move(YOU, "checks failing", 2)
    if r.get("review") == "changes" and not requested:
        by = ", ".join(r.get("changes", [])) or "a reviewer"
        if r.get("reviewed") and r.get("pushed", 0) > r["reviewed"]:
            return Move(YOU, f"pushed since {by} requested changes; review not re-requested", 3)
        return Move(YOU, f"changes requested by {by}" + (f" ({open_threads})" if threads else ""), 3)
    if requested:
        return Move(", ".join(requested), "review requested", 4)
    for d in tr.blockers(t):
        if d.kind == "decision":
            return decision_move(tr, d.rec, coordinated)
        if d.kind == "action":  # the user's to do: this side's
            return Move(YOU, f"{d.ident} open", 5, human=True)
        if d.kind == "external":
            note = link_title(d.link).partition(" — ")[2] if d.link else ""
            return Move(d.ident, short(note, 60) if note else "", 6)
    if threads:
        return Move(YOU, f"{approved}; {open_threads}" if approved else open_threads, 7)
    if approved:
        if r.get("checks") == "running":
            return Move("CI", "checks running", 8)
        state = {"behind": "branch behind its base", "blocked": "merge blocked by the repo's rules"}.get(
            r.get("merge", ""), "ready to merge")
        return Move(YOU, f"{approved}; {state}", 8)
    if r.get("draft"):
        return Move(YOU, "draft", 9, quiet=True)
    if is_open:
        return Move(YOU, "no review requested", 9)
    return unread_link_move(tr, t) or (Move(YOU, "PR closed", 10) if t.pr_state == "closed"
                                       else Move(YOU, "no PR yet", 10, quiet=True))
