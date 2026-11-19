"""Whose move a ticket under way waits on: this side (the user and the AI) or the people or thing named. Computed from
the review facts `sync` reads, the ticket's blockers and the items still open in the log (`open_items`), with the
clock (MOVE_RULE); nothing writes it, nor whether an item is stuck."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace

from .markdown import Link
from .schema import COORDINATOR, DEADLINE_S, parse_day, stage_of, utc_seconds
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
    "title": "the PR's title, for the views; no move reads it",
    "base": "the branch the PR would merge into; a base that is other tickets' branch makes the ticket wait on them "
            "(computed; depends_on does not change)",
}
YOU = "you"
MOVE_RULE = (
    "A ticket under way has a move: whose turn it waits on, computed, never written. In order: merge conflicts, "
    "failing checks or requested changes not re-requested are yours; a requested review is the reviewer's; a "
    "submitted plan or built code is its reviewer's (the coordinator's, else a human's) until reviewed; findings of "
    "a review not yet fixed are yours; an open decision it waits on is its owner's: a human by name, the session on "
    "a ticket, or the coordinator, and a human's when no owner is named and no coordinator is watching; an open "
    "action yours; an external blocker is that blocker's; then unresolved review threads, an approval (CI's while "
    "its checks run), a draft or a PR with no review requested are yours. With several open PRs, the most urgent of "
    "their moves, yours first, naming that PR. A submission or a question that is no human's move turns stuck, and "
    "a human's, once left unanswered past its deadline, or at once on a ticket under way that no session is on. "
    "`next` holds your own next action: do not write a wait on a reviewer into it.")


@dataclass
class Move:
    who: str  # YOU (also for an open action), the reviewers, "CI", an open decision's (decision_move) or a blocker's id
    what: str = ""  # why it is their move, as a state: "review requested", "checks failing"
    rank: int = 0  # its rule's place in MOVE_RULE: the more urgent first
    quiet: bool = False  # your own work going on as planned, where nothing has stopped to wait for you
    human: bool = False  # the move is a human's to make: an open action, or a decision a human must answer
    stuck_from: Move | None = None  # the move it had until its open item turned stuck (`open_items`); None: not stuck

    @property
    def mine(self) -> bool:
        return self.who == YOU

    def text(self, sep: str = ": ") -> str:
        return self.who + (sep + self.what if self.what else "")


# ---------------------------------------------------------------- submissions and reviews
# `tracker submit` hands a ticket's plan or built code (SUBMIT_WHAT) to its reviewer, `tracker review` answers it: both
# are events on the ticket, and `handoffs` reads where each stands from them.

@dataclass(frozen=True)
class Handed:
    """What a submission hands over, in words."""
    noun: str  # what it is
    review: str  # its review's name
    ready: str  # the submission's line, after the ticket's id


HANDED = {"plan": Handed("plan", "plan review", "plan ready for review"),  # each of SUBMIT_WHAT
          "code": Handed("built code", "plan conformance check", "built code ready for the plan conformance check")}


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


# ---------------------------------------------------------------- open items
# What waits for a reply, from the log and the records: a submission (its review), the findings of a review (their
# fix), a question (its answer), a wait on a human (its end). Each has a move, and is stuck once nobody whose move it
# is has acted in time (DEADLINE_S): worked out by whatever reads it, with the clock, and never stored.

@dataclass
class Item:
    kind: str  # "submission", "findings", "question" or "wait"
    key: str  # stable while it is open: "submission:<seq>", "findings:<review seq>", "question:<D-id>", "wait:<seq>"
    refs: list[str]  # the tickets it concerns
    at: float  # since when it has waited on its move's party (epoch s): its writing, or a later hand-over
    what: str  # one line for a person
    move: Move  # whose turn it is
    stuck: bool = False  # nobody whose turn it is has acted in time: a human's move now
    session: str = ""  # for a wait: the session that waits
    asker: str = ""  # for a wait: "" or the subagent's id


def count(n: int, word: str) -> str:
    """`1 finding`, `2 findings`."""
    return f"{n} {word}{'s' * (n != 1)}"


def created(r: Record, now: float) -> float:
    """When a record was made: its `created_at`, a date alone as that day's start; `now` with neither."""
    at = utc_seconds(r.get("created_at"))
    if at is not None:
        return at
    day = parse_day(r.get("created_at"))
    return time.mktime(day.timetuple()) if day else now


def question_key(ident: str) -> str:
    """An open decision's Item key."""
    return f"question:{ident}"


def open_items(tr: Tracker, events: list[dict], now: float, coordinated: bool, on: set[str],
               live: dict[str, str], coordinator_since: float = 0) -> list[Item]:
    """The items open at `now`, stuck first, then the oldest first, from one pass over `events` (the log, oldest
    first). `coordinated`: a coordinator watches the tracker, since `coordinator_since`; `on`: the tickets the live
    sessions have under way; `live`: the sessions running now, each with its name. A submission is open until a
    review answers it, a review's findings until each is marked fixed or the next submission comes, both while the
    ticket is open; a question while its decision is open; a wait until its end, while its session runs.
    Stuck: a wait past its deadline; a submission or a question whose move is not a human's past its deadline, or at
    once when it concerns a ticket under way that no live session is on, and its move is then a human's. One whose
    move is a human's already is simply theirs; findings never are; a ticket not yet started makes nothing stuck. A
    deadline runs from when the item became its party's move: a coordinator that starts watching, a question passed
    to a new owner or a ticket that starts is not blamed for the time before."""
    def judged(item: Item, late: str) -> Item:
        if item.move.human:
            return item
        alone = [r for r in item.refs if (t := tr.lookup(r)) and t.in_flight and r not in on]
        if now - item.at > DEADLINE_S[item.kind]:
            what = late
        elif alone:
            what = f"{item.what}, and no session is on {', '.join(alone)}"
        else:
            return item
        move = Move("human", f"{item.move.what}, stuck with {item.move.who}", item.move.rank, human=True,
                    stuck_from=item.move)
        return replace(item, stuck=True, what=what, move=move)

    def minutes(at: float) -> str:
        return f"{int((now - at) // 60)} min"

    items = []
    for (ident, what), h in handoffs(events).items():
        t, handed = tr.lookup(ident), HANDED.get(what)
        if not t or t.kind != "ticket" or t.closed or not handed:
            continue
        if h.submission:
            at = utc_seconds(h.submission.get("at")) or now
            if coordinated:
                at, move = max(at, coordinator_since), Move(COORDINATOR, handed.review, 5)
            else:
                move = Move("human", handed.review, 5, human=True)
            items.append(judged(Item("submission", f"submission:{h.submission['seq']}", [t.id], at,
                                     f"{t.id} {handed.ready}", move),
                                f"{move.who} has not reviewed {t.id}'s {handed.noun} for {minutes(at)}"))
        elif h.review and h.review.get("verdict") == "changes":
            left = [n for n in finding_numbers(h.review) if n not in h.fixed]
            said = f"{count(len(left), 'finding')} of the {handed.review} open"
            if left:
                items.append(Item("findings", f"findings:{h.review['seq']}", [t.id],
                                  utc_seconds(h.review.get("at")) or now, f"{t.id}: {said}", Move(YOU, said, 5)))
    owned = {e.get("decision"): utc_seconds(e.get("at")) or 0 for e in events
             if e["kind"] == "decision.updated" and "owner" in e}  # each decision's last change of owner
    started = {e["refs"][0]: utc_seconds(e.get("at")) or 0 for e in events  # each ticket's last start
               if e["kind"] == "status" and e["refs"] and stage_of("ticket", e.get("status")).in_flight}
    for d in tr.open_decisions():
        kind, name = owner_kind(tr, d)
        refs = sorted(set(tr.touched_by(d)) | ({name} if kind == "ticket" else set()))
        move = decision_move(tr, d, coordinated)
        at = max(created(d, now), owned.get(d.id, 0),
                 coordinator_since if move.who == COORDINATOR else 0,
                 started.get(name, 0) if kind == "ticket" else 0)
        items.append(judged(Item("question", question_key(d.id), refs, at, f"{d.id} open", move),
                            f"{move.who} has not answered {d.id} for {minutes(at)}"))
    waits, ended = {}, set()
    for e in events:
        if e["kind"] == "human.wait":
            waits[(e.get("session"), e.get("asker"))] = e  # a later one replaces it: a hook that failed left it
        elif e["kind"] == "human.wait.end":
            ended.add((e.get("session"), e.get("of")))  # its own session's: `of` numbers one tracker's events
    for (sid, asker), e in waits.items():
        if (sid, e["seq"]) in ended or sid not in live:
            continue
        at = utc_seconds(e.get("at")) or now
        late = now - at > DEADLINE_S["wait"]
        what = (f"{live[sid]} has waited on a human for {int(now - at)} s" if late
                else f"{live[sid]} waits on a human" + (f": {e['why']}" if e.get("why") else ""))
        items.append(Item("wait", f"wait:{e['seq']}", list(e["refs"]), at, what,
                          Move("human", "asked for a permission or an answer", 5, human=True), late, sid,
                          str(asker or "")))
    return sorted(items, key=lambda i: (not i.stuck, i.at))


def whose_move(tr: Tracker, t: Record, coordinated: bool = False, items: list[Item] = ()) -> Move | None:
    """The move of a ticket under way (MOVE_RULE); None for any other ticket, and while `sync` has read the review facts
    of none of its open PRs. `coordinated`: a coordinator watches the tracker; `items`: the open items (`open_items`).
    `session.move_of` gives both."""
    if not t.in_flight:
        return None
    open_prs = tr.open_prs(t)
    if not open_prs:
        return pr_move(tr, t, {}, coordinated, items)
    moves = [(n, pr_move(tr, t, r, coordinated, items)) for n, r in open_prs if r is not None]
    if not moves:
        return None
    number, move = min(moves, key=lambda x: (not x[1].mine, x[1].rank))
    if len(open_prs) == 1:
        return move

    def named(m: Move) -> Move:  # the PR it is about; the move it was before it turned stuck too
        return replace(m, what=f"#{number} {m.what}".strip())
    return replace(named(move), stuck_from=move.stuck_from and named(move.stuck_from))


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


def pr_move(tr: Tracker, t: Record, r: dict, coordinated: bool, items: list[Item] = ()) -> Move:
    """A ticket's move as one open PR's facts make it, and the open items (`open_items`): an item's move, stuck or
    not, is the ticket's. Empty facts, for a ticket with no open PR."""
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
    handed = next((i for i in items if i.kind in ("submission", "findings") and t.id in i.refs), None)
    if handed:
        return handed.move
    for d in tr.blockers(t):
        if d.kind == "decision":
            asked = next((i for i in items if i.key == question_key(d.rec.id)), None)
            return asked.move if asked else decision_move(tr, d.rec, coordinated)
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
