"""What a UI shows, as data: the facts each view works out once, and `tracker data` and `tracker list --json`, the only
API a UI reads. Text goes out as it is in the files (Markdown stays Markdown), so each UI draws it its own way."""

from __future__ import annotations

import datetime as dt
import time

from .markdown import headings, section_block, strip_comments, without_section, Link
from .model import (FACTS, LIVE_SYNC_S, README_SECTIONS, SCALES, SPANS, STAGES, all_trackers, archived_trackers,
    branch_entry, day_text, due_date, level, level_name, is_pr_link, linked_prs, sequence, show_value, sort_key, span,
    whose_move, Dep, Move, Record, Tracker)
from .session import ago, live_by_tracker, live_sessions, match_cwd, Live
from .contract import check
from .github import pr_url
from .views import dep_facts, duration, span_lines, stage_counts
from .watcher import in_use, watcher_of

VIEW_SCHEMA = 4  # a UI that reads another number asks to be updated


def ticket_state(tr: Tracker, t: Record) -> str:
    """A ticket's state: dropped or closed; under way, stackable, ready or blocked; else its unknown status."""
    return ("dropped" if t.dropped else "closed" if t.closed else "going" if t.in_flight
            else tr.gate(t).state or t.stage)


def dep_tone(d: Dep) -> str:
    """The status colour of a dependency: met, under way, or in the way."""
    return "done" if d.done else "stack" if d.rec and d.rec.in_flight else "blocked"


def filter_counts(tr: Tracker) -> list[tuple[str, int]]:
    """The sequence's filters in the order they show, each with how many tickets it keeps."""
    counts = stage_counts(tr)
    blocked = sum(1 for t in tr.tickets if not t.closed and tr.blockers(t))
    return [("blocked", blocked), *((s, counts[s]) for s in STAGES if s in counts)]


def drawn_links(edges: dict[str, list[str]]) -> dict[str, list[str]]:
    """Per ticket, the tickets it waits on (Sequence.waits) that no longer chain implies (a transitive reduction):
    the graph draws only these, and the Deps column still lists every one."""
    below: dict[str, set[str]] = {}

    def under(i: str) -> set[str]:
        """Every ticket i waits on, through any chain; a cycle stops the walk."""
        if i not in below:
            below[i] = set()
            below[i] = set().union(*({j} | under(j) for j in edges[i]))
        return below[i]
    return {i: [j for j in deps if not any(j in under(o) for o in deps if o != j)] for i, deps in edges.items()}


def graph_order(tr: Tracker, edges: dict[str, list[str]], step: dict[str, int]) -> list[Record]:
    """The tickets in depth-first dependency order, as `git log --topo-order`: each after every ticket it waits on,
    and the tickets that this one lets start right after it, so a chain stays together; the lowest id first among
    those ready. Tickets in or after a cycle, then dropped tickets, go last, in step order."""
    live = {t.id for t in tr.tickets if not t.dropped}
    waits = {i: [j for j in edges[i] if j in live] for i in live}
    left = {i: len(js) for i, js in waits.items()}  # per ticket: the tickets it waits on not yet placed
    after: dict[str, list[str]] = {i: [] for i in live}  # per ticket: the tickets that wait on it
    for i, js in waits.items():
        for j in js:
            after[j].append(i)
    stack = sorted((i for i in live if not left[i]), key=sort_key, reverse=True)
    out: list[str] = []
    while stack:
        i = stack.pop()
        out.append(i)
        for k in after[i]:
            left[k] -= 1
        stack += sorted((k for k in after[i] if not left[k]), key=sort_key, reverse=True)
    place = {i: n for n, i in enumerate(out)}
    return sorted(tr.tickets, key=lambda t: (t.id not in place, place.get(t.id, 0), t.dropped,
                                             step[t.id], sort_key(t.id)))


def agent_word(s: Live) -> str:
    """`working since 09:52`: an agent session's status, and since when (a clock time: the view does not change while
    the status holds)."""
    word = {"busy": "working", "waiting": "needs you"}.get(s.status, s.status or "running")
    return f"{word} since {clock(s.since)}" if s.since else word


def clock(ts: float) -> str:
    """`09:52` today, `Tue 09:52` on another day."""
    same_day = time.localtime(ts)[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if same_day else "%a %H:%M", time.localtime(ts))


def due_text(d: dt.date) -> str:
    """`today`, `tomorrow`, a weekday within the week ahead, else `14 Oct` (with the year when not this one)."""
    days = (d - dt.date.today()).days
    if days in (0, 1):
        return ("today", "tomorrow")[days]
    if 1 < days < 7:
        return f"{d:%a}"
    return f"{d.day} {d:%b}" + (f" {d.year}" if d.year != dt.date.today().year else "")


def move_view(move: Move | None) -> dict | None:
    return {"text": move.text(), "mine": move.mine, "quiet": move.quiet} if move else None


def dep_view(d: Dep) -> dict:
    """A dependency: `ref` when it names a record here; `title`, an external blocker's reason."""
    return {"id": d.ident, "done": d.done, "tone": dep_tone(d), "ref": bool(d.rec),
            "title": d.link.text if d.link and not d.rec else ""}


def pr_views(tr: Tracker, t: Record) -> list[dict]:
    """A ticket's PRs, each once (GitHub's repo names ignore case): the ones `sync` follows, with their state, then each
    of its `PR:` links to a PR's page, also one in a repo `sync` does not read. `text`: what its link says of it."""
    prs = {}
    for n, state in t.prs.items():
        url = pr_url(tr.repo_of(t), n)
        prs[url.lower() or str(n)] = {"number": str(n), "url": url, "state": state or "", "text": ""}
    for link in linked_prs(t):
        pr = prs.setdefault(link.url.lower(), {"number": link.number, "url": link.url, "state": "", "text": ""})
        pr["text"] = link.text
    return list(prs.values())


def link_views(items: list[Link]) -> list[dict]:
    return [{"label": x.label, "text": x.text, "sub": list(x.sub)} for x in items]


def time_view(t: Record) -> list[dict]:
    """A ticket's wait and cycle time (SPANS), those it has."""
    return [{"name": name, "text": duration(x)} for name in SPANS if (x := span(t, name)) is not None]


def record_view(tr: Tracker, r: Record) -> dict:
    """What a record shows when it opens: its lead lines (its next or summary), what it waits on and what waits on it
    (dep_facts, as the text views give them), its facts and links, then its body in Markdown."""
    g = tr.gate(r)
    deps = [{"label": label, "text": v} if isinstance(v, str) else {"label": label, "items": [dep_view(d) for d in v]}
            for label, v in dep_facts(tr, r)]
    if r.kind == "ticket" and tr.decisions_for(r):
        deps.append({"label": "decisions", "items": [
            {"id": d.id, "done": d.closed, "tone": "done" if d.closed else "blocked", "ref": True, "title": ""}
            for d in tr.decisions_for(r)]})
    move = whose_move(tr, r) if r.kind == "ticket" else None
    word, line = ("summary", r.get("summary")) if r.closed else ("next", r.get("next"))
    return {
        "id": r.id, "title": str(r.get("title")), "stage": r.stage,
        "state": ticket_state(tr, r) if r.kind == "ticket" else r.stage,
        "gate": {"state": g.state, "text": g.text()},
        "time": time_view(r) if r.kind == "ticket" else [],
        "line": {"label": word, "text": str(line)} if line else None,
        "move": move_view(move),
        "prs": pr_views(tr, r) if r.kind == "ticket" else [],
        "deps": deps,
        "facts": [{"label": k.removesuffix("_at"), "text": show_value(k, r.get(k)), "code": k == "branch"}
                  for k in FACTS if r.get(k)],
        "links": link_views([x for x in r.links if not is_pr_link(x)]),  # a PR's link shows in its row of `prs`
        "body": strip_comments(without_section(r.body, "Links")).strip(),
    }


def ticket_view(tr: Tracker, t: Record, order: int, step: int, links: list[str]) -> dict:
    """A ticket as its row shows it. A closed ticket's waits-on is history, which its row leaves out (the opened ticket
    still lists it). `sort`: its value per sortable column; a None or "" sorts last."""
    waits = [] if t.closed else tr.deps(t)
    unblocks = [o.id for o in tr.waiting_on(t.id)]
    return {
        "id": t.id, "title": str(t.get("title")), "stage": t.stage, "closed": t.closed, "state": ticket_state(tr, t),
        "gate": {"state": tr.gate(t).state, "text": tr.gate(t).text()},
        "order": order, "step": step, "links": links,
        "group": str(t.get("group", "")), "levels": {key: level_name(t, key) for key in SCALES},
        "waits": [dep_view(d) for d in waits], "unblocks": unblocks,
        "sort": {"step": order, "ticket": t.id,
                 "status": list(STAGES).index(t.stage) if t.stage in STAGES else len(STAGES),
                 "group": str(t.get("group", "")), **{key: level(t, key) for key in SCALES},
                 "waits": len(waits), "unblocks": len(unblocks)},
    }


def action_view(tr: Tracker, a: Record) -> dict:
    """An action's row: its status, what it blocks while open, and when open its due day (`late`: `overdue` once past,
    `today` on the day); when closed, when it closed."""
    due = due_date(a)
    late = "" if not due else "overdue" if due < dt.date.today() else "today" if due == dt.date.today() else ""
    return {"id": a.id, "title": str(a.get("title")), "status": str(a.get("status", "open")), "closed": a.closed,
            "gate": tr.gate(a).text(),
            "when": day_text(a.get("closed_at")) if a.closed else "",
            "due": {"text": due_text(due), "late": late} if due and not a.closed else None}


def decision_view(tr: Tracker, d: Record) -> dict:
    return {"id": d.id, "title": str(d.get("title")), "status": str(d.get("status", "open")), "gate": tr.gate(d).text()}


def now_view(tr: Tracker) -> dict:
    """Work under way (your move first, then the others' by who, then the tickets whose PR GitHub has not been read
    for; in progress before in review within each), each branch's handoff, the agent sessions on no ticket, and the
    watch."""
    state = tr.state()
    marks, handoffs = state.get("synced", {}), state.get("handoff", {})
    moves = {t.id: whose_move(tr, t) for t in tr.tickets if t.in_flight}

    # The sessions `tracker start` tied to this tracker that run on this machine: a mark by each ticket they have
    # under way; a session with none gets its own entry.
    live = [(x, match_cwd(x.cwd, x.sid, tr)) for x in live_sessions(tr.slug)]
    agents = {i: [x for x, m in live if i in (r.id for r in m.active)] for i in moves}

    def agent_views(sessions: list[Live]) -> list[dict]:
        return [{"busy": x.status == "busy", "waiting": x.status == "waiting", "word": agent_word(x), "cwd": x.cwd}
                for x in sessions]

    def turn(t: Record) -> tuple:
        move = moves[t.id]
        return (2 if not move else 0 if move.mine else 1, move.rank if move and move.mine else 0,
                move.who if move else "", t.stage != "in-progress")

    items = []
    for t in sorted((t for t in tr.tickets if t.id in moves), key=turn):
        b = str(t.get("branch") or "")
        mark = branch_entry(marks, tr.repo_of(t), b)
        fresh = f"commits logged {ago(mark['at'])}" if mark else ""
        move = moves[t.id]
        items.append({"key": t.id, "kind": "ticket", "id": t.id, "stage": t.stage, "state": ticket_state(tr, t),
                      "prs": pr_views(tr, t), "fresh": fresh, "move": move_view(move),
                      "line": f"next: {t.get('next')}" if t.get("next") else "", "agents": agent_views(agents[t.id])})
    for key, h in handoffs.items():
        items.append({"key": f"handoff-{key}", "kind": "handoff", "title": "Handoff", "line": h.get("text", ""),
                      "agents": []})
    for x, m in live:
        if not m.active:
            on = [{"id": t.id, "stage": t.stage, "state": ticket_state(tr, t)} for t in m.focus]
            items.append({"key": f"agent-{x.sid}", "kind": "agent", "on": on, "agents": agent_views([x])})
    idle = None
    if not any(t.in_flight for t in tr.tickets) and tr.meta.get("status") == "active":
        idle = {"ready": [t.id for t in tr.ready()]}
    w = watcher_of(tr)  # `tracker watch`: who watches the tracker for the user, or when the watch ended
    watch = None
    if w:
        watch = {"running": w["running"], "text": f"Watched by {w['who']} since {clock(w['since'])}" if w["running"]
                 else f"Watch by {w['who']} ended {clock(w['ended'])}"}
    return {"items": items, "idle": idle, "watch": watch}


def issue_state(tr: Tracker) -> str:
    """What the live line says of the issue fields: when they were last read, or what a Refresh waits on. Empty when no
    ticket has an Issue link."""
    linked = [t for t in tr.tickets if t.aliases]
    if not linked:
        return ""
    read, asked = tr.issue_reads()
    if asked and any(not t.closed for t in tr.issues_to_read()):
        who = ("waiting for a session to read them" if live_sessions(tr.slug)
               else "no session on this tracker to read them")
        return f"issues: asked; {who}"
    last = max((read[t.id] for t in linked if read.get(t.id)), default=0)
    return f"issues read {clock(last)}" if last else "issues not read yet"


def reference_view(tr: Tracker) -> dict:
    """What is settled or past: closed decisions and actions, the rest of the README, the log."""
    closed = sorted((a for a in tr.actions if a.closed),
                    key=lambda a: (str(a.get("closed_at")), sort_key(a.id)), reverse=True)
    extra = tr.readme_body
    for h in README_SECTIONS:
        extra = without_section(extra, h)
    extra = strip_comments(extra).strip()
    log_path = tr.root / "log.md"
    log = [ln[2:] for ln in log_path.read_text().splitlines() if ln.startswith("- ")] if log_path.exists() else []
    return {"decisions": [decision_view(tr, d) for d in tr.decisions if d.closed],
            "actions": [action_view(tr, a) for a in closed],
            "more": {"body": extra} if headings(extra) else None,
            "log": log[::-1]}


def view_model(tr: Tracker) -> dict:
    """One tracker, as `tracker data` gives it: all a UI shows of it, and `records`, each record by id as it opens."""
    errors, warnings = check(tr)
    seq = sequence(tr)
    links = drawn_links(seq.waits)
    goal = strip_comments(section_block(tr.readme_body, "Goal") + section_block(tr.readme_body, "Scope")).strip()
    return {
        "schema": VIEW_SCHEMA, "slug": tr.slug, "title": tr.title, "archived": tr.archived,
        "facts": {k.removesuffix("_at"): show_value(k, tr.meta[k]) if tr.meta.get(k) else ""
                  for k in ("status", "owner", "created_at")},
        "synced": int(tr.raw_state().get("last_sync", 0)), "sync_every": LIVE_SYNC_S, "issues": issue_state(tr),
        "links": link_views(tr.context), "goal": {"headings": headings(goal), "body": goal} if goal else None,
        "check": {"errors": errors, "warnings": warnings},
        "actions": [action_view(tr, a) for a in tr.open_actions()],
        "now": now_view(tr),
        "scales": list(SCALES), "filters": filter_counts(tr), "spans": span_lines(tr),
        "tickets": [ticket_view(tr, t, i, seq.step[t.id], links[t.id])
                    for i, t in enumerate(graph_order(tr, seq.waits, seq.step))],
        "decisions": [decision_view(tr, d) for d in tr.open_decisions()],
        "reference": reference_view(tr),
        "records": {r.id: record_view(tr, r) for r in (*tr.tickets, *tr.decisions, *tr.actions)},
    }


def trackers_view() -> dict:
    """Every tracker for a tracker menu, and what keeps each from the archive or trash (`in_use`)."""
    live = live_by_tracker()
    return {
        "schema": VIEW_SCHEMA,
        "active": [{"slug": t.slug, "title": t.title, "root": str(t.root), "in_use": in_use(t, live=live)}
                   for t in all_trackers()],
        "archived": [{"slug": t.slug, "title": t.title, "root": str(t.root)} for t in archived_trackers()],
    }
