"""Plain-text views for the AI and the terminal: the index, a record's context, and a session's brief."""

from __future__ import annotations

import re
import time
from pathlib import Path
from statistics import median

from .model import (BIN, CLOSED_TICKET, ISOLATION_RULE, SPANS, STAGES, TEXT_MAX, cut, link_lines, resolution, sequence,
    short, span, utc_seconds, whose_move, Record, Start, Tracker)
from .session import ago, branch_handoff, cwd_repo, handoff_line, lag, Match
from .contract import check

# ---------------------------------------------------------------- views

def pr_label(t: Record) -> str:
    pr = t.get("pr")
    if not pr:
        return "—"
    state = t.get("pr_state")
    return f"#{pr}" + (f" {state}" if state and state != "merged" else "")


def index_lines(tr: Tracker, stages: set[str] | None = None, group: str | None = None,
                titles: bool = False, width: int = 0) -> list[str]:
    counts = stage_counts(tr)
    tally = " · ".join(f"{s} {counts[s]}" for s in STAGES if s in counts)
    out = [f"{tr.headline()} · {len(tr.tickets)} tickets · {tally}"]
    if tr.context:
        out += ["Context:", *link_lines(tr.context, tr.root, titles=titles, width=width), ""]
    rows = [t for t in tr.tickets
            if (not stages or t.stage in stages) and (group is None or str(t.get("group")) == group)]
    w_id = max([len(t.id) for t in rows] + [2])
    w_group = max([len(str(t.get("group") or "—")) for t in rows] + [1])
    for t in rows:
        closed = t.stage in CLOSED_TICKET
        nxt = " ".join(str((not closed and t.get("next")) or t.get("summary") or t.get("title")).split())
        nxt = gate(tr, t) + (nxt if len(nxt) <= 90 else nxt[:89] + "…")
        out.append(f"{t.id:<{w_id}}  {str(t.get('group') or '—'):<{w_group}}  {t.stage:<11} {pr_label(t):<12} {nxt}")
    moves = move_lines(tr) if not stages or stages & {"in-progress", "in-review"} else []
    out += [""] + moves + [""] * bool(moves) + order_lines(tr)
    decisions = tr.open_decisions()
    if decisions:
        out.append("")
        out.append("Open decisions:")
        for d in decisions:
            touched = ", ".join(tr.touched_by(d))
            out.append(f"{d.id:<{w_id}}  {d.get('title')}" + (f"  ({touched})" if touched else ""))
    return out


def move_lines(tr: Tracker) -> list[str]:
    """The tickets under way by whose move (`whose_move`), yours first: each move once, with its tickets."""
    moves: dict[str, tuple[tuple, list[str]]] = {}  # text: (order, tickets)
    unread = []
    for t in tr.tickets:
        move = whose_move(tr, t)
        if move:
            moves.setdefault(move.text(), ((not move.mine, move.rank, move.text()), []))[1].append(t.id)
        elif t.stage in ("in-progress", "in-review"):
            unread.append(t.id)
    if not moves and not unread:
        return []
    synced = tr.raw_state().get("last_sync")
    out = ["Whose move" + (f" (GitHub read {ago(synced)})" if synced and tr.reviews else "") + ":"]
    out += [f"  {text} — {', '.join(ids)}" for text, (_, ids) in sorted(moves.items(), key=lambda m: m[1][0])]
    return out + ([f"  not read from GitHub yet (`tracker sync`) — {', '.join(unread)}"] if unread else [])


def stage_counts(tr: Tracker) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tr.tickets:
        counts[t.stage] = counts.get(t.stage, 0) + 1
    return counts


def gate(tr: Tracker, t: Record) -> str:
    """`[waits T-5, D-10] `, `[stacks on T-4] ` (it waits only on work under way) or `[ready] ` for an unfinished
    ticket."""
    if t.stage in CLOSED_TICKET:
        return ""
    blockers = tr.blockers(t)
    if blockers:
        return f"[{'stacks on' if tr.stackable(t) else 'waits'} {', '.join(d.ident for d in blockers)}] "
    return "[ready] " if t.stage == "todo" else ""


def start_text(tr: Tracker, s: Start) -> str:
    """Where a ticket's work starts (START_RULE): `stack on <branch> (T-4 in-review, #12; rev: review requested)`,
    the separate branches to choose from, or the default branch."""
    if s.base:
        b = s.base
        where = f"stack on {b.get('branch')}" if b.get("branch") else f"stack on {b.id}'s work (no branch recorded)"
        move = whose_move(tr, b)
        facts = [f"{b.id} {b.stage}" + (f", #{b.get('pr')}" if b.get("pr") else ""), *([move.text()] if move else [])]
        return f"{where} ({'; '.join(facts)})"
    if s.apart:
        on = " and ".join(f"{b.id} ({b.get('branch') or 'no branch'})" for b in s.apart)
        return f"{on} are on separate branches: stack on one, or wait until one merges"
    return "from the default branch"


def order_lines(tr: Tracker) -> list[str]:
    seq = sequence(tr)
    starts = tr.startable()
    out = [f"Ready to start: {', '.join(t.id for t, s in starts if not s.stacked) or 'nothing'}"]
    stacked: dict[str, list[str]] = {}  # the ticket(s) under way: the tickets that can stack on them
    for t, s in starts:
        if s.stacked:
            stacked.setdefault(s.base.id if s.base else " or ".join(b.id for b in s.apart), []).append(t.id)
    if stacked:
        out.append("Can start stacked on work under way: " + "; ".join(f"{', '.join(ids)} on {on}"
                                                                     for on, ids in stacked.items())
                   + " (`tracker ready` names the branch)")
    if seq.critical:
        out.append(f"Critical path (longest chain of unfinished tickets): {' → '.join(seq.critical)}")
    out += [f"Dependency cycle: {' → '.join(c)}" for c in seq.cycles]
    return out


def dep_lines(tr: Tracker, rec: Record) -> list[str]:
    out = []
    deps = tr.deps(rec) if rec.kind == "ticket" else []
    if deps:
        out.append("waits on: " + "; ".join(d.describe() for d in deps))
    blockers = [d.ident for d in deps if not d.done]
    start = tr.start_point(rec) if rec.kind == "ticket" else None
    if start and start.stacked:
        out.append(f"start: {start_text(tr, start)}")
    elif rec.kind == "ticket" and blockers and rec.stage not in CLOSED_TICKET:
        out.append("blocked by: " + ", ".join(blockers))
    elif rec.kind == "ticket" and rec.stage == "todo":
        out.append("blocked by: nothing — ready to start")
    later = tr.waiting_on(rec.id)
    if later:
        verb = "unblocks" if rec.kind == "ticket" else "blocks"
        out.append(f"{verb}: " + ", ".join(f"{t.id} {t.stage}" for t in later))
    if rec.kind == "decision" and rec.list("refs"):
        out.append("touches: " + ", ".join(rec.list("refs")))
    return out


CHAIN_CARRY_FORWARD_MAX = 12  # bullets from tickets beyond the direct dependencies; `context --deep` shows all


def ancestors(tr: Tracker, t: Record) -> list[Record]:
    """Every ticket a ticket waits on, directly or through others, nearest first."""
    out, queue, seen = [], [t], {t.id}
    while queue:
        for d in tr.deps(queue.pop(0)):
            if d.kind == "ticket" and d.ident not in seen:
                seen.add(d.ident)
                out.append(d.rec)
                queue.append(d.rec)
    return out


LOG_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2})(?: \[([^\]]*)\])? ")
DECISION_LOG = re.compile(r"(?:Opened|Decided) (D-\d+)\b")  # the lines `decide` writes that the decision holds
CONTEXT_LOG = 3  # log lines `context` shows by default


def log_for(tr: Tracker, ident: str, n: int, shown: set[str] = frozenset()) -> list[str]:
    """The last n log lines whose refs name this id. A line that opened or settled a decision in `shown` is left out:
    the view shows that decision."""
    path = tr.root / "log.md"
    if n <= 0 or not path.exists():
        return []
    return [ln[2:] for ln in path.read_text().splitlines()
            if (m := LOG_LINE.match(ln)) and ident in (m[2] or "").split()
            and not ((d := DECISION_LOG.match(ln, m.end())) and d[1] in shown)][-n:]


def recent_log(tr: Tracker, rec: Record, n: int, width: int = 0, shown: set[str] = frozenset()) -> list[str]:
    lines = log_for(tr, rec.id, n, shown)
    return ["", f"Recent log for {rec.id} (oldest first; `find {rec.id}` for all):",
            *(f"  {cut(x, width)}" for x in lines)] if lines else []


def ticket_lines(tr: Tracker, rec: Record, handoff: bool = True, width: int = 0) -> list[str]:
    """A ticket's own facts: its line, branch, next action or summary, handoff, dependencies and links."""
    head = [rec.id, rec.get("title"), rec.get("group") and f"group {rec.get('group')}", rec.stage,
            f"PR {pr_label(rec)}"]
    out = [" · ".join(str(x) for x in head if x)]
    closed = rec.stage in CLOSED_TICKET
    for k in ("branch", "repo", "summary" if closed else "next"):
        if rec.get(k):
            out.append(f"{k}: {rec.get(k)}")
    move = whose_move(tr, rec)
    if move:
        out.append(f"move: {move.text(' — ')}")
    if handoff and not closed and (h := branch_handoff(tr, tr.repo_of(rec), str(rec.get("branch")))):
        out.append(handoff_line(h))
    out += dep_lines(tr, rec)
    if rec.links:
        out += ["links:", *link_lines(rec.links, tr.root, width=width)]
    return out


def carry_lines(tr: Tracker, tickets: list[Record], deep: bool = False, limit: int = 0) -> list[str]:
    """What the tickets build on, each fact once: their direct dependencies' Carry forward, then the rest of the
    chain's. `limit` (a brief) caps the direct bullets at that many characters and names the rest of the chain only."""
    direct = list({d.ident: d.rec for t in tickets for d in tr.deps(t) if d.kind == "ticket"}.values())
    which = tickets[0].id if len(tickets) == 1 else "<id>"
    out = []
    cf = [(t.id, b) for t in direct for b in t.carry_forward]
    shown, size = [], 0
    for i, b in cf:
        size += len(i) + len(b) + 4
        if limit and size > limit and shown:
            break
        shown.append((i, b))
    if shown:
        out += ["", "Carry forward from dependencies:", *(f"  {i}: {b}" for i, b in shown)]
    if len(shown) < len(cf):
        rest = list(dict.fromkeys(i for i, _ in cf[len(shown):]))
        out.append(f"  … {len(cf) - len(shown)} more from {', '.join(rest)} (`tracker context {which}`)")
    seen = {t.id for t in tickets + direct}
    earlier = [(a.id, b) for a in {a.id: a for t in tickets for a in ancestors(tr, t) if a.id not in seen}.values()
               for b in a.carry_forward]
    if earlier and limit:
        ids = ", ".join(dict.fromkeys(i for i, _ in earlier))
        out += ["", f"Earlier in the chain: {len(earlier)} Carry forward bullets from {ids} "
                    f"(`tracker context {which} --deep`)."]
    elif earlier:
        shown = earlier if deep else earlier[:CHAIN_CARRY_FORWARD_MAX]
        out += ["", "Carry forward from earlier in the chain (what the dependencies built on):"]
        out += [f"  {i}: {b}" for i, b in shown]
        if len(shown) < len(earlier):
            rest = list(dict.fromkeys(i for i, _ in earlier[len(shown):]))
            out.append(f"  … {len(earlier) - len(shown)} more from {', '.join(rest)} "
                       f"(`tracker context {which} --deep`)")
    return out


def decision_lines(tr: Tracker, tickets: list[Record], width: int = 0) -> list[str]:
    """The decisions that touch the tickets, each once: the open ones, then the settled ones, which hold. `width` (a
    brief) cuts each settled line and leaves out the open ones' paths."""
    ids = [t.id for t in tickets]
    who = " and ".join(ids) if len(ids) < 3 else "these tickets"
    decisions = list({d.id: d for t in tickets for d in tr.decisions_for(t)}.values())
    out = []
    open_d = [d for d in decisions if d.get("status", "open") == "open"]
    if open_d:
        out += ["", f"Open decisions touching {who}:"]
        out += [f"  {d.id}: {d.get('title')}" + ("" if width else f"  ({d.path})") for d in open_d]
    settled = [d for d in decisions if d.get("status") == "closed"]
    if settled:
        out += ["", f"Settled decisions touching {who} (they hold; `context D-<n>` for the detail):"]
        out += ["  " + cut(f"{d.id}: {d.get('title')} → {resolution(d)}", width) for d in settled]
    return out


def context_lines(tr: Tracker, rec: Record, full: bool, deep: bool = False, log: int = CONTEXT_LOG) -> list[str]:
    if rec.kind == "decision":
        out = [f"{rec.id} · {rec.get('status', 'open')} · {rec.get('title')}", *dep_lines(tr, rec)]
        if rec.get("status") == "closed":
            out.append(f"resolution: {resolution(rec)}")
    else:
        out = ticket_lines(tr, rec) + carry_lines(tr, [rec], deep) + decision_lines(tr, [rec])
    shown = {d.id for d in tr.decisions_for(rec)} if rec.kind == "ticket" else {rec.id}
    out += recent_log(tr, rec, log, shown=shown)
    out += ["", f"file: {rec.path}"]
    if full:
        out += ["", rec.body.strip()]
    return out


def protocol(slug: str) -> str:
    """The protocol every brief carries: each event and the command that records it. The skill holds the rest."""
    most = {k: v[0] for k, v in TEXT_MAX.items()}
    return "\n".join([
        f"Tracker protocol (CLI `tracker`; fallback `{BIN.as_posix()} --tracker {slug} <command>`):",
        f"- {ISOLATION_RULE}",
        "- read a record with `context <id>`, or its own text with `show <ids> --section <name>`, not its file",
        "- change a section: one line `add <id> <section> \"...\"` or `drop`; the whole `put <id> <section> -` with a "
        "heredoc (<<'EOF'). Any text can be `-`, from stdin: quotes and backticks pass as they are",
        "- start a ticket: `context <id>` (a `start:` line names the branch to start from), then "
        "`set <id> status=in-progress`",
        "- the hooks log each commit on the branch of a ticket under way. A step ends or `next` changes: "
        "`step <id> --next \"...\"`, with a message only for what the commits do not say (a result, a measurement, "
        "why) and `--carry \"...\"` for each fact a later ticket needs. `next` is your own next action: a `move:` "
        "line shows what the PR waits on. "
        f"Limits in characters: the message {most['log']}, `next` {most['next']}, `--done` {most['summary']}, each "
        f"`--carry` {most['carry']}; the detail goes in the PR",
        "- a ticket ends: `step <id> \"...\" --done \"<what it delivered>\"`; you stop mid-work (a pause, a "
        "compaction, the session's end): `step <id> \"...\" --pause \"<state and next step>\"`",
        "- as they happen: a direction choice `decide`; an order or blocker `wait`; a result file "
        "`attach <file> --ref <ids>`",
        "- a subagent writes no tracker (a hook tells it): put what it needs in its prompt (`context <id> --brief`), "
        "and record what it reports",
        "- `[work-tracker]` hook lines are the tracker's requests: act on each in the same turn. A write prints the "
        "`check` problems it adds; `step`, `decide`, `wait`, `new` and `attach` log themselves, so no `log` for the "
        "same fact. The work-tracker `tracker` skill is the full protocol.",
    ])


HOW = {"branch": "", "use": " (chosen with `tracker use`)",
       "name": " (matched by the id in the branch name; if wrong, `tracker use <id>`)"}
CHOSEN = " (this session's choice, from `tracker start --on`; `tracker start <slug>` clears it)"
PICK_ONE = ("More than one ticket is under way here. When this session's work is one of them, "
            "`tracker start --on <id>` puts the session on it alone: the brief, the hooks, and `step` and `set` "
            "without an id then use it.")
BRANCH_RULE = ("Change git branches only when the user asks. When the user asks for a branch for a ticket, run "
               "`tracker context <id>` first, start the branch from the one its `start:` line names (else the "
               "default branch), and name it for the work, led by the ticket's Issue id when it has one "
               "(`feat/<ISSUE>-<slug>`); `tracker set <id> status=in-progress` then records it on the ticket, which "
               "ties this session to it.")


# A brief goes into every session's context: each fact once, and small. `tracker context <id>` has the rest.
BRIEF_TICKETS_MAX = 3  # tickets under way shown in full; the rest get one line each
BRIEF_CARRY_CHARS = 6000  # the dependencies' Carry forward, about 1.5K tokens
BRIEF_LINE_CHARS = 200  # a Context, link, log or settled-decision line
BRIEF_LOG = 2  # log lines per ticket
SPAN_RECENT_S = 7 * 86400  # a span line's recent window


def duration(seconds: float) -> str:
    """`42 min` under an hour (`1 min` at least), `26 h` under two days, then `2.4 d`."""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h"
    return f"{seconds / 86400:.1f} d"


def span_lines(tr: Tracker, now: float | None = None) -> list[str]:
    """A line per span (SPANS) that some ticket has: the median, the fastest ticket, and the median of the spans that
    ended in the last 7 days."""
    now = time.time() if now is None else now
    lines = []
    for name, (_, end, what) in SPANS.items():
        spans = [(t, x) for t in tr.tickets if (x := span(t, name)) is not None]
        if not spans:
            continue
        fastest = min(spans, key=lambda tx: tx[1])
        parts = [f"{name.capitalize()} time ({what}): median {duration(median(x for _, x in spans))} over "
                 f"{len(spans)}", f"fastest {duration(fastest[1])} ({fastest[0].id})"]
        recent = [x for t, x in spans if now - (utc_seconds(t.get(end)) or 0) <= SPAN_RECENT_S]
        if recent:
            parts.append(f"last 7 days: median {duration(median(recent))} over {len(recent)}")
        lines.append(" · ".join(parts))
    return lines


ISSUE_DUE_SHOWN = 10  # due tickets named in the request for their issue fields; the rest as a count


def issue_request(tr: Tracker) -> str:
    """The request for the issue fields that are due (STATE_RULES["issues"]), or "". The model reads them with the
    issue tracker's tool; the tracker cannot."""
    due = tr.issue_due()
    if not due:
        return ""
    ids = ", ".join(t.id for t in due[:ISSUE_DUE_SHOWN]) + (f" +{len(due) - ISSUE_DUE_SHOWN}"
                                                             if len(due) > ISSUE_DUE_SHOWN else "")
    return (f"[work-tracker] Issue fields due for {len(due)} ticket(s): {ids} (`tracker issue --due` gives their issue "
            "links). If a tool for their issue tracker is available (an MCP server for Shortcut, Jira, Linear …), "
            "read each issue's priority and creation time and record them: `tracker issue <id> --priority \"<the "
            "tracker's word>\" --created <ISO 8601 time>`, or `tracker issue <id>` when it has neither. With no such "
            "tool, leave them and never guess a value.")


def brief(m: Match, cwd: str | Path, synced: list[str] | None = None, note: str = "", compact: bool = False,
          full: bool = False, with_protocol: bool = True) -> str:
    """What a session needs to work, most urgent first: where it is, the handoff, work the tracker may not show yet,
    then the tickets under way (the branch's other tickets in one line) with what they build on and the decisions
    that touch them, or, with none, what can start. `compact` (after a compaction) names the Context documents
    without their links. `full` adds the tickets' bodies."""
    tr = m.tracker
    if m.tickets:
        how = CHOSEN if m.chosen else HOW[m.how]
        parts = [f"[work-tracker] {tr.slug} — {tr.headline()}. Branch {m.branch}: {m.summary()}{how}."]
    else:
        parts = [f"[work-tracker] {tr.slug} — {tr.headline()}. This session is on no ticket"
                 + (f" (branch {m.branch})" if m.branch else "") + ". Ticket ids resolve in this tracker. "
                 + BRANCH_RULE]
    h = branch_handoff(tr, cwd_repo(cwd), m.branch)
    if h:
        parts.append(handoff_line(h, "Handoff"))
    if note:
        parts.append(note)
    work = lag(m, cwd)
    if work:
        parts.append(f"The tracker may lag the work: {work.text()}. Bring it up to date from what you know: "
                     "`step <id> --next \"...\"`, with a message only for what the commit subjects do not say"
                     + (" (it logs the commits)." if work.commits else "."))
    focus = m.focus
    if len(m.active) > 1 and not m.chosen:
        parts.append(PICK_ONE)
    if focus:
        if tr.context:
            parts.append("\n".join(["Context (open the one that governs a choice before making it; `tracker index` "
                                    "gives the detail):",
                                    *link_lines(tr.context, tr.root, titles=compact, width=BRIEF_LINE_CHARS)]))
        shown, rest = focus[:BRIEF_TICKETS_MAX], focus[BRIEF_TICKETS_MAX:]
        decided = {d.id for t in shown for d in tr.decisions_for(t)}  # shown below with their answers
        for t in shown:
            parts.append("\n".join(ticket_lines(tr, t, handoff=False, width=BRIEF_LINE_CHARS)
                                   + recent_log(tr, t, BRIEF_LOG, BRIEF_LINE_CHARS, decided) + ["", f"file: {t.path}"]
                                   + (["", t.body.strip()] if full else [])))
        if rest:
            parts.append("\n".join(["Also under way (`tracker context <id>` for each):",
                                    *(f"  {t.id} {t.stage}" + (f" ({mv.text()})" if (mv := whose_move(tr, t)) else "")
                                      + f": {short(t.get('next') or t.get('title'), 120)}" for t in rest)]))
        shared = carry_lines(tr, shown, limit=BRIEF_CARRY_CHARS) + decision_lines(tr, shown, BRIEF_LINE_CHARS)
        if shared:
            parts.append("\n".join(shared).strip("\n"))
    else:
        if m.tickets:
            ready = [t.id for t, _ in tr.startable()]
            parts.append(f"No open ticket on {m.branch}. To start one here: `tracker set <id> status=in-progress`"
                         + (f" (can start: {', '.join(ready)})" if ready else "") + ".")
        parts.append("\n".join(index_lines(tr, set(STAGES) - CLOSED_TICKET, titles=compact, width=BRIEF_LINE_CHARS)))
    if synced:
        parts.append("Synced from GitHub: " + "; ".join(synced))
    ask = issue_request(tr)
    if ask:
        parts.append(ask)
    errors, warnings = check(tr)
    if errors or warnings:
        parts.append(f"Tracker check: {len(errors)} errors, {len(warnings)} warnings — run `check`.")
    if with_protocol:
        parts.append(protocol(tr.slug))
    return "\n\n".join(parts)
