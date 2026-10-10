"""Plain-text views for the AI and the terminal: the index, a record's context, and a session's brief."""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from statistics import median

from .markdown import section, strip_comments
from .model import (BIN, ISOLATION_RULE, KINDS, LOG_LINE, NO_STAGE, OPEN_STAGES, README_INSTRUCTIONS, SCALES, SPANS,
    STAGES, STALE_ACTION_DAYS, STEP_MESSAGE, TEXT_MAX, cut, days_since, due_date, link_lines, logged_decision,
    resolution, sequence, short, show_value, span, utc_seconds, whose_move, Dep, Record, Start, Tracker)
from .git import cwd_repo
from .session import ago, branch_handoff, handoff_line, lag, Match
from .contract import check

# ---------------------------------------------------------------- views

def pr_label(t: Record) -> str:
    pr = t.get("pr")
    if not pr:
        return "—"
    state = t.get("pr_state")
    return f"#{pr}" + (f" {state}" if state and state != "merged" else "")


def index_lines(tr: Tracker, stages: set[str] | None = None, group: str | None = None,
                titles: bool = False, width: int = 0, urls: bool = True) -> list[str]:
    counts = stage_counts(tr)
    tally = " · ".join(f"{s} {counts[s]}" for s in STAGES if s in counts)
    out = [f"{tr.headline()} · {len(tr.tickets)} tickets · {tally}"]
    if tr.context:
        out += ["Context" + ("" if urls else " (`tracker show tracker --section context` gives the links)") + ":",
                *link_lines(tr.context, tr.root, titles=titles, width=width, urls=urls), ""]
    acts = action_lines(tr, width)
    out += acts + [""] * bool(acts)
    rows = [t for t in tr.tickets
            if (not stages or t.stage in stages) and (group is None or str(t.get("group")) == group)]
    w_id = max([len(t.id) for t in rows] + [2])
    w_group = max([len(str(t.get("group") or "—")) for t in rows] + [1])
    for t in rows:
        nxt = gate(tr, t) + short((not t.closed and t.get("next")) or t.get("summary") or t.get("title"), 90)
        out.append(f"{t.id:<{w_id}}  {str(t.get('group') or '—'):<{w_group}}  {t.stage:<11} {pr_label(t):<12} {nxt}")
    moves = move_lines(tr) if not stages or any(STAGES.get(s, NO_STAGE).in_flight for s in stages) else []
    out += [""] + moves + [""] * bool(moves) + order_lines(tr)
    decisions = tr.open_decisions()
    if decisions:
        out.append("")
        out.append("Open decisions:")
        for d in decisions:
            touched = ", ".join(tr.touched_by(d))
            out.append(f"{d.id:<{w_id}}  {d.get('title')}" + (f"  ({touched})" if touched else ""))
    return out


def action_lines(tr: Tracker, width: int = 0) -> list[str]:
    """The user's open actions, the soonest due first, each with what it concerns, the tickets it blocks and its due
    day. One past its due day, or with none and open longer than STALE_ACTION_DAYS, says to ask the user."""
    acts = tr.open_actions()
    if not acts:
        return []
    out = ["Open actions for the user (not yours to do; when the user says one is done or no longer needed: "
           "`act A-<n> --done` or `--drop`):"]
    for a in acts:
        age, due = days_since(a.get("created_at")), due_date(a)
        ask = ": ask the user whether it is done"
        stale = (f" (due {due}" + (f", overdue{ask}" if due < dt.date.today() else "") + ")" if due
                 else f" (open {age} days{ask})" if age > STALE_ACTION_DAYS else "")
        refs, blocks = ", ".join(a.list("refs")), ", ".join(t.id for t in tr.waiting_on(a.id))
        out.append("  " + cut(f"{a.id}{stale}: {a.get('title')}" + (f" ({refs})" if refs else "")
                              + (f"; blocks {blocks}" if blocks else ""), width))
    return out


def move_lines(tr: Tracker) -> list[str]:
    """The tickets under way by whose move (`whose_move`), yours first: each move once, with its tickets."""
    moves: dict[str, tuple[tuple, list[str]]] = {}  # text: (order, tickets)
    unread = []
    for t in tr.tickets:
        move = whose_move(tr, t)
        if move:
            moves.setdefault(move.text(), ((not move.mine, move.rank, move.text()), []))[1].append(t.id)
        elif t.in_flight:
            unread.append(t.id)
    if not moves and not unread:
        return []
    synced = tr.raw_state().get("last_sync")
    out = ["Whose move" + (f" (GitHub read {ago(synced)})" if synced and tr.prs else "") + ":"]
    out += [f"  {text} — {', '.join(ids)}" for text, (_, ids) in sorted(moves.items(), key=lambda m: m[1][0])]
    return out + ([f"  not read from GitHub yet (`tracker sync`) — {', '.join(unread)}"] if unread else [])


def stage_counts(tr: Tracker) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tr.tickets:
        counts[t.stage] = counts.get(t.stage, 0) + 1
    return counts


def gate(tr: Tracker, t: Record) -> str:
    """`[waits on T-5, D-10] `, `[stacks on T-4] ` (it waits only on work under way) or `[ready] ` (Tracker.gate)."""
    g = tr.gate(t)
    return f"[{g.text()}] " if g.state else ""


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


def dep_facts(tr: Tracker, rec: Record) -> list[tuple[str, list[Dep] | str]]:
    """What a record waits on and what waits on it, as the text views (dep_lines) and the viewer both show it: a
    ticket's dependencies and where it can start stacked, the tickets that wait on the record, and the records it
    touches (refs). Each as (label, the records it names, or a text)."""
    out: list[tuple[str, list[Dep] | str]] = []
    if rec.kind == "ticket" and tr.deps(rec):
        out.append(("waits on", tr.deps(rec)))
    start = tr.start_point(rec) if rec.kind == "ticket" else None
    if start and start.stacked:
        out.append(("start", start_text(tr, start)))
    if later := tr.waiting_on(rec.id):
        out.append(("unblocks" if rec.kind == "ticket" else "blocks", [Dep(t.id, t) for t in later]))
    if refs := rec.list("refs"):
        out.append(("touches", [Dep(r.id, r) if (r := tr.lookup(x)) else Dep(x) for x in refs]))
    return out


def dep_lines(tr: Tracker, rec: Record) -> list[str]:
    """dep_facts as lines, each record with its state; after what a ticket waits on, what still blocks it."""
    facts = dep_facts(tr, rec)
    out = [f"{label}: {v if isinstance(v, str) else '; '.join(d.describe() for d in v)}" for label, v in facts]
    g, stacked = tr.gate(rec), any(label == "start" for label, _ in facts)
    line = ("blocked by: " + ", ".join(g.ids) if g.state in ("stack", "blocked") and not stacked
            else "blocked by: nothing — ready to start" if g.state == "ready" else "")
    if line:
        out.insert(1 if facts and facts[0][0] == "waits on" else 0, line)
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


CONTEXT_LOG = 3  # log lines `context` shows by default


HISTORY_LAST = 30  # log lines `history` shows by default


def history_lines(tr: Tracker, refs: list[str], since: str, last: int, shown: set[str] = frozenset()) -> list[str]:
    """The log's last `last` lines (0: all), oldest first: those whose refs name any of `refs`, dated `since` or
    later. A line that opened or settled a decision in `shown` is left out: the view shows that decision."""
    path = tr.root / "log.md"
    lines = [ln[2:] for ln in (path.read_text().splitlines() if path.exists() else [])
             if (m := LOG_LINE.match(ln)) and m[1] >= since
             and (not refs or set(refs) & set((m[2] or "").split()))
             and logged_decision(ln[m.end():]) not in shown]
    return lines[-last:] if last > 0 else lines


def recent_log(tr: Tracker, rec: Record, n: int, width: int = 0, shown: set[str] = frozenset()) -> list[str]:
    lines = history_lines(tr, [rec.id], "", n, shown) if n > 0 else []
    return ["", f"Recent log for {rec.id} (oldest first; `history --ref {rec.id} --last 0` for all):",
            *(f"  {cut(x, width)}" for x in lines)] if lines else []


def ticket_lines(tr: Tracker, rec: Record, handoff: bool = True, width: int = 0) -> list[str]:
    """A ticket's own facts: its line, branch, repo, due day, next action or summary, move, handoff, dependencies and
    links."""
    head = [rec.id, rec.get("title"), rec.get("group") and f"group {rec.get('group')}", rec.stage,
            f"PR {pr_label(rec)}"]
    out = [" · ".join(str(x) for x in head if x)]
    closed = rec.closed
    for k in ("branch", "repo", "due", "summary" if closed else "next"):
        if rec.get(k):
            out.append(f"{k}: {show_value(k, rec.get(k))}")
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
    out += more_line(cf, len(shown), f"tracker context {which}")
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
        out += more_line(earlier, len(shown), f"tracker context {which} --deep")
    return out


def more_line(pairs: list[tuple[str, str]], shown: int, command: str) -> list[str]:
    """`  … 3 more from T-1, T-2 (`<command>`)` for the (ticket, bullet) pairs past the first `shown`."""
    rest = list(dict.fromkeys(i for i, _ in pairs[shown:]))
    return [f"  … {len(pairs) - shown} more from {', '.join(rest)} (`{command}`)"] if rest else []


def decision_lines(tr: Tracker, tickets: list[Record], width: int = 0) -> list[str]:
    """The decisions that touch the tickets, each once: the open ones, then the settled ones, which hold. `width` (a
    brief) cuts each settled line and leaves out the open ones' paths."""
    ids = [t.id for t in tickets]
    who = " and ".join(ids) if len(ids) < 3 else "these tickets"
    decisions = list({d.id: d for t in tickets for d in tr.decisions_for(t)}.values())
    out = []
    open_d = [d for d in decisions if not d.closed]
    if open_d:
        out += ["", f"Open decisions touching {who}:"]
        out += [f"  {d.id}: {d.get('title')}" + ("" if width else f"  ({d.path})") for d in open_d]
    settled = [d for d in decisions if d.closed]
    if settled:
        out += ["", f"Settled decisions touching {who} (they hold; `context D-<n>` for the detail):"]
        out += ["  " + cut(f"{d.id}: {d.get('title')} → {resolution(d)}", width) for d in settled]
    return out


def context_lines(tr: Tracker, rec: Record, full: bool, deep: bool = False, log: int = CONTEXT_LOG) -> list[str]:
    if rec.kind == "ticket":
        out = ticket_lines(tr, rec) + carry_lines(tr, [rec], deep) + decision_lines(tr, [rec])
    else:
        out = [f"{rec.id} · {rec.get('status')} · {rec.get('title')}", *dep_lines(tr, rec)]
        if rec.closed and KINDS[rec.kind].closing:
            out.append(f"{KINDS[rec.kind].closing.lower()}: {resolution(rec)}")
        elif rec.get("due") and not rec.closed:
            out.append(f"due: {rec.get('due')}")
    shown = {d.id for d in tr.decisions_for(rec)} if rec.kind == "ticket" else {rec.id}
    out += recent_log(tr, rec, log, shown=shown)
    out += ["", f"file: {rec.path}"]
    if full:
        out += ["", rec.body.strip()]
    return out


def protocol(slug: str) -> str:
    """The protocol each session-start brief carries: each event and the command that records it. The skill holds
    the rest."""
    most = {k: v[0] for k, v in TEXT_MAX.items()}
    return "\n".join([
        f"Tracker protocol (CLI `tracker`; fallback `{BIN.as_posix()} --tracker {slug} <command>`):",
        f"- {ISOLATION_RULE}",
        "- read a record with `context <id>`, or its own text with `show <ids> --section <name>`, not its file",
        "- change a section: one line `add <id> <section> \"...\"` or `drop`; the whole `put <id> <section> -` with a "
        "heredoc (<<'EOF'). Any text but a title, a name or a link can be `-`, from stdin: quotes and backticks pass "
        "as they are",
        "- start a ticket: `context <id>` (a `start:` line names the branch to start from), then "
        "`set <id> status=in-progress`",
        "- the hooks log each commit on the branch of a ticket under way. A step ends or `next` changes: "
        f"`step <id> --next \"...\"`, with {STEP_MESSAGE}, and `--carry \"...\"` for each fact a later ticket "
        "needs. `next` is your own next action: a `move:` "
        "line shows what the PR waits on. "
        f"Limits in characters: the message {most['log']}, `next` {most['next']}, `--done` {most['summary']}, each "
        f"`--carry` {most['carry']}; the detail goes in the PR",
        "- a ticket ends: `step <id> \"...\" --done \"<what it delivered>\"`; you stop mid-work (a pause, a "
        "compaction, the session's end): `step <id> \"...\" --pause \"<state and next step>\"`",
        "- as they happen: a direction choice `decide`; an order or blocker `wait`; a result file "
        "`attach <file> --ref <ids>`; a task only the user should do (talk to a person, an access, a sign-off): ask "
        "them, then `act \"<what, with whom>\" --refs <ids>`",
        "- a subagent writes no tracker (a hook tells it): put what it needs in its prompt (`context <id> --brief`), "
        "and record what it reports",
        "- `[work-tracker]` hook lines are the tracker's requests: act on each in the same turn. A write prints the "
        "`check` problems it adds and logs what the history needs, so no `log` for the same fact. The work-tracker "
        "`tracker` skill is the full protocol.",
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
BRIEF_INSTRUCTIONS_CHARS = 2000  # README ## Instructions, about 500 tokens; `show` has the rest
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


ISSUE_SHOWN = 5  # tickets named in the request for their issue fields; the rest as a count
ISSUE_HOW = ("Read each issue's priority, estimate, due day and creation time with its issue tracker's tool and record "
             "them: `tracker issue <id> " + " ".join(f"--{k} <{s.span}: {s.ends}>" for k, s in SCALES.items())
             + " --due <YYYY-MM-DD> --created <ISO 8601 time>`. Map each by what it means on its issue tracker's own "
             "scale, not by a raw number its API gives (`tracker rules` gives the mapping). Leave out a flag whose "
             "field the issue tracker lacks; pass it empty when the issue's value means none. Never guess a value.")


def issue_request(tr: Tracker, first: list[Record] = ()) -> str:
    """The request for the issue fields to read (STATE_RULES["issues"]), or "": one line, the `first` tickets (the
    session's own) named first. `tracker issue` gives the links and how to record them (ISSUE_HOW). The model reads
    them with the issue tracker's tool; the tracker cannot."""
    due = sorted(tr.issues_to_read(), key=lambda t: t not in first)
    if not due:
        return ""
    ids = ", ".join(t.id for t in due[:ISSUE_SHOWN]) + (f" +{len(due) - ISSUE_SHOWN}" if len(due) > ISSUE_SHOWN else "")
    return (f"[work-tracker] Issue fields to read for {len(due)} ticket(s): {ids}. With a tool for their issue tracker "
            "(such as an MCP server), run `tracker issue` for their links and how to record them; with none, leave "
            "them: never guess a value.")


def instructions(tr: Tracker) -> str:
    """The README's ## Instructions for the brief: this work's standing rules for the agent, cut to
    BRIEF_INSTRUCTIONS_CHARS."""
    text = strip_comments(section(tr.readme_body, README_INSTRUCTIONS)).strip()
    if not text:
        return ""
    if len(text) > BRIEF_INSTRUCTIONS_CHARS:
        text = (text[:BRIEF_INSTRUCTIONS_CHARS].rsplit("\n", 1)[0] + "\n… the rest: `tracker show tracker --section "
                f"{README_INSTRUCTIONS.lower()}`")
    return f"This tracker's instructions (README ## {README_INSTRUCTIONS}; follow them):\n{text}"


def check_lines(tr: Tracker, tickets: list[Record]) -> str:
    """`check` for a brief: the errors as a count (each one is to fix), and the warnings on this session's tickets
    and the README in full. The rest wait for `check`, and each write prints the problems it adds."""
    errors, warnings = check(tr)
    ids = {t.id for t in tickets} | {"README.md"}
    mine = [w for w in warnings if w.split(":", 1)[0] in ids]
    if not errors and not mine:
        return ""
    return "\n".join([f"Tracker check: {len(errors)} error(s) — run `check` and fix them." if errors
                      else "Tracker check, on this work:", *(f"  ⚠ {short(w, BRIEF_LINE_CHARS)}" for w in mine)])


def brief(m: Match, cwd: str | Path, synced: list[str] | None = None, note: str = "", compact: bool = False,
          full: bool = False, with_protocol: bool = True) -> str:
    """What a session needs to work, most urgent first: where it is, the handoff, work the tracker may not show yet,
    then the tickets under way (the branch's other tickets in one line) with what they build on and the decisions
    that touch them, or, with none, what can start. `compact` (after a compaction) gives each Context line its label
    and title only. `full` adds the tickets' bodies."""
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
                     f"`step <id> --next \"...\"`, with {STEP_MESSAGE}"
                     + (" (it logs the commits)." if work.commits else "."))
    focus = m.focus
    if len(m.active) > 1 and not m.chosen:
        parts.append(PICK_ONE)
    rules = instructions(tr)
    if rules:
        parts.append(rules)
    if focus:
        if tr.context:
            parts.append("\n".join(["Context (open the one that governs a choice before making it; `tracker show "
                                    "tracker --section context` gives the links):",
                                    *link_lines(tr.context, tr.root, titles=compact, width=BRIEF_LINE_CHARS,
                                                urls=False)]))
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
        acts = action_lines(tr, BRIEF_LINE_CHARS)  # the index below has them when no ticket is under way
        if acts:
            parts.append("\n".join(acts))
    else:
        if m.tickets:
            ready = [t.id for t, _ in tr.startable()]
            parts.append(f"No open ticket on {m.branch}. To start one here: `tracker set <id> status=in-progress`"
                         + (f" (can start: {', '.join(ready)})" if ready else "") + ".")
        parts.append("\n".join(index_lines(tr, OPEN_STAGES, titles=compact, width=BRIEF_LINE_CHARS, urls=False)))
    if synced:
        parts.append("Synced from GitHub: " + "; ".join(synced))
    ask = issue_request(tr, focus)
    if ask:
        parts.append(ask)
    problems = check_lines(tr, focus)
    if problems:
        parts.append(problems)
    if with_protocol:
        parts.append(protocol(tr.slug))
    return "\n\n".join(parts)
