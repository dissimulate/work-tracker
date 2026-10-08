"""The live viewer: the page's HTML, and the localhost server that serves it and restarts on new code."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time

from .markdown import headings, section_block, strip_comments, without_section, Link
from .model import (CLI, CLOSED_TICKET, EVIDENCE_DIR, HOME, IN_FLIGHT, LIST_KEYS, PACKAGE, PYTHON, README_SECTIONS,
    ROOT, SPANS, STAGES, WINDOWS, all_trackers, atomic_write, branch_entry, files_hash, locked, priority_rank, sequence,
    sort_key, span, spawn, tracker_at, whose_move, Dep, Move, Record, Tracker)
from .session import ago, live_sessions, match_cwd, Live
from .contract import check
from .views import duration, pr_label, span_lines, stage_counts, start_text
from .github import sync
from .watcher import watcher_of

# ---------------------------------------------------------------- render

def md_inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\s][^*]*)\*(?!\w)", r"<em>\1</em>", s)
    # One pass for both forms: a bare URL inside a link's URL must not become a second link, whose quotes would end
    # the first one's href and open its tag to new attributes.
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)|(?<![\"'>=])(https?://[^\s<)\"']+)",
                  lambda m: link_html(m[2], m[1]) if m[1] else link_html(m[3], m[3]), s)


SAFE_HREF = re.compile(rf"(?:https?://|mailto:|#|{EVIDENCE_DIR}/)", re.I)


def link_html(url: str, text: str) -> str:
    """A link for already-escaped text; a URL that is not http(s), mailto, an in-page anchor or an evidence/ file
    stays plain text."""
    if not SAFE_HREF.match(url):
        return text
    return f'<a href="{url.replace(chr(34), "&quot;")}">{text}</a>'


def md_to_html(md: str) -> str:
    """A small Markdown subset: headings, lists (nested by indent), code fences, tables, paragraphs."""
    out, para, lines, i = [], [], strip_comments(md).splitlines(), 0

    def flush():
        if para:
            out.append("<p>" + md_inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
        elif m := re.match(r"(#{1,6})\s+(.*)", line):
            flush()
            n = min(len(m.group(1)) + 1, 6)
            out.append(f"<h{n}>{md_inline(m.group(2))}</h{n}>")
        elif re.match(r"\s*[-*]\s+", line):
            flush()
            depth = 0
            while i < len(lines) and (m := re.match(r"(\s*)[-*]\s+(.*)", lines[i])):
                d = len(m.group(1)) // 2 + 1
                while depth < d:
                    out.append("<ul>")
                    depth += 1
                while depth > d:
                    out.append("</ul>")
                    depth -= 1
                out.append("<li>" + md_inline(m.group(2)) + "</li>")
                i += 1
            out += ["</ul>"] * depth
            continue
        elif line.lstrip().startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                head, *body = rows
                out.append("<div class=tbl><table><tr>" + "".join(f"<th>{md_inline(c)}</th>" for c in head) + "</tr>")
                out += ["<tr>" + "".join(f"<td>{md_inline(c)}</td>" for c in r) + "</tr>" for r in body]
                out.append("</table></div>")
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


VIEWER_DIR = ROOT / "viewer"
ASSET_TYPES = {".css": "text/css", ".js": "text/javascript"}
EVIDENCE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".svg": "image/svg+xml", ".pdf": "application/pdf", ".html": "text/plain; charset=utf-8"}


def props_html(groups: list[list[tuple[str, str]]]) -> str:
    """Label and value rows in one grid, so the values line up; each group after the first starts with a gap."""
    rows = "".join(f'<dt{" class=gap" if i and not j else ""}>{html.escape(label)}</dt><dd>{value}</dd>'
                   for i, group in enumerate(g for g in groups if g) for j, (label, value) in enumerate(group))
    return f"<dl class=props>{rows}</dl>" if rows else ""


def link_rows(items: list[Link]) -> list[tuple[str, str]]:
    return [(x.label, md_inline(x.text) + "".join(f"<br><small>{md_inline(y)}</small>" for y in x.sub))
            for x in items]


def chip(tone: str, text: str = "") -> str:
    """A status pill: `tone` is a status or s-ready, s-stack, s-blocked (viewer/style.css, "status colours")."""
    return f'<span class="chip s-{html.escape(tone)}">{html.escape(text or tone)}</span>'


def move_chip(move: Move | None) -> str:
    """Whose move a ticket under way waits on: yours stands out, another's does not."""
    return f'<span class="chip move s-{"you" if move.mine else "them"}">{html.escape(move.text())}</span>' \
        if move else ""


def agent_word(s: Live) -> str:
    """`working since 09:52`: a Claude session's status, and since when (a clock time: the page does not change
    while the status holds)."""
    word = {"busy": "working"}.get(s.status, s.status or "running")
    return f"{word} since {clock(s.since)}" if s.since else word


def clock(ts: float) -> str:
    """`09:52` today, `Tue 09:52` on another day."""
    same_day = time.localtime(ts)[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if same_day else "%a %H:%M", time.localtime(ts))


def agent_icon(sessions: list[Live]) -> str:
    """Left of a Now row, the Claude sessions on its work: a ring that spins while one of them works (busy) and stays
    still while all are idle (they wait for the user)."""
    if not sessions:
        return ""
    word = "working" if any(x.status == "busy" for x in sessions) else "idle"
    title = "; ".join(f"{x.name}: {agent_word(x)}" for x in sessions)
    return (f'<span class="agent{" busy" * (word == "working")}" role="img" aria-label="agent {word}" '
            f'title="{html.escape(title)}"></span>')


def version(tr: Tracker) -> str:
    """`<data>.<code>.<synced>`: the first part changes with any tracker file, the sessions running on it or its
    watcher (the page swaps its content), the second with any viewer/ file or this script (the page reloads), the
    third is the last GitHub sync (epoch s)."""
    live = json.dumps([[x.sid, x.status, x.since, x.branch, x.focus] for x in live_sessions(tr.slug)]
                      + [watcher_of(tr)])
    code = files_hash([*sorted(VIEWER_DIR.iterdir()), *sorted(PACKAGE.glob("*.py"))])
    return f"{files_hash(tr.data_files(), live)}.{code}.{int(tr.raw_state().get('last_sync', 0))}"


LIST_CH = 24  # the viewer's sequence: a list column shows the items that fit, then `+n`
STALE_MARK_S = 86400  # the viewer's Now: a branch whose commits were last logged before this shows it unopened


def main_html(tr: Tracker) -> str:
    e = html.escape
    counts = stage_counts(tr)
    seq = sequence(tr)

    def panel(key: str, head: str, body: str, line: str = "", attrs: str = "") -> str:
        """Every part of the page that opens: its head, then `line` cut to one line while it is closed."""
        nx = f"<span class=nx>{line}</span>" if line else ""
        return f'<details data-id="{e(key)}"{attrs}><summary>{head}{nx}</summary><div>{body}</div></details>'

    def named(name: str, meta: str = "") -> str:
        return f"<b>{name}</b>" + (f"<span class=meta>{meta}</span>" if meta else "")

    def sec(key: str, title: str, count: int | str, body: str, start_open: bool = False) -> str:
        """A section: its heading opens and closes it. The count shows while it is closed."""
        return panel(f"_sec-{key}", f'<h2>{title}{f" · {count}" if count else ""}</h2>', body,
                     attrs=" class=sec" + " open" * start_open)

    def ref(ident: str) -> str:
        return f'<a class=id href="#{e(ident)}">{e(ident)}</a>'

    def toned(tone: str, body: str, attrs: str = "") -> str:
        """Text in a status colour (a dependency, a decision); a link or id in it takes the same colour."""
        return f'<span class="tone s-{tone}"{attrs}>{body}</span>'

    def dep_html(d: Dep) -> str:
        if not d.rec:
            return toned("blocked", e(d.ident), f' title="{e(d.link.text)}"' if d.link else "")
        tone = "done" if d.done else "stack" if d.kind == "ticket" and d.rec.stage in IN_FLIGHT else "blocked"
        return toned(tone, ref(d.ident) + " ✓" * d.done)

    def later_html(ident: str) -> str:
        return ", ".join(ref(t.id) for t in tr.waiting_on(ident))

    def gate_html(r: Record) -> tuple[str, str]:
        """(filter tag, summary chip): what a ticket waits on, or that it is ready; what an open decision blocks."""
        if r.kind == "decision":
            later = [t.id for t in tr.waiting_on(r.id)]
            return "", chip("blocked", f"blocks {', '.join(later)}") if later and r.get("status") == "open" else ""
        if r.stage in CLOSED_TICKET:
            return "", ""
        blockers = tr.blockers(r)
        if blockers:
            tone, verb = ("stack", "stacks on") if tr.stackable(r) else ("blocked", "waits on")
            return "blocked", chip(tone, f"{verb} {', '.join(d.ident for d in blockers)}")
        return ("ready", chip("ready")) if r.stage == "todo" else ("", "")

    def body_html(r: Record, lead: tuple[str, str] | None = None) -> str:
        """What a ticket or decision shows when it opens: one grid of its line (next or summary), dependencies,
        facts and links, then the body."""
        deps = [("waits on", ", ".join(dep_html(d) for d in tr.deps(r)))] if r.kind == "ticket" and tr.deps(r) else []
        start = tr.start_point(r) if r.kind == "ticket" else None
        if start and start.stacked:
            deps.append(("start", e(start_text(tr, start))))
        if tr.waiting_on(r.id):
            deps.append(("unblocks" if r.kind == "ticket" else "blocks", later_html(r.id)))
        if r.kind == "ticket" and tr.decisions_for(r):
            deps.append(("decisions", ", ".join(
                toned("closed", ref(d.id) + " ✓") if d.get("status") == "closed" else toned("blocked", ref(d.id))
                for d in tr.decisions_for(r))))
        facts = []
        for k in ("branch", "base", "repo", "group", "refs", "owner", "started_at", "merged_at", "updated"):
            if r.get(k):
                value = (", ".join(ref(x) for x in r.list(k)) if k == "refs"
                         else e(", ".join(r.list(k)) if k in LIST_KEYS else str(r.get(k))))
                facts.append((k.removesuffix("_at"), f"<code>{value}</code>" if k in ("branch", "base") else value))
        move = whose_move(tr, r) if r.kind == "ticket" else None
        head = ([lead] if lead else []) + ([("move", move_chip(move))] if move else [])
        props = props_html([head, deps, facts, link_rows(r.links)])
        return f'<div>{props}{md_to_html(without_section(r.body, "Links"))}</div>'

    def decision_html(d: Record) -> str:
        return panel(d.id, f'<span class=id>{e(d.id)}</span>{chip(d.get("status", "open"))}{gate_html(d)[1]}'
                           f'<b>{e(str(d.get("title")))}</b>', body_html(d))

    # A ticket's two list columns, as (text, html) per item. A closed ticket's waits-on is history: its row leaves it
    # out; the opened ticket still lists it.
    def waits_items(t: Record) -> list[tuple[str, str]]:
        return [] if t.stage in CLOSED_TICKET else [(d.ident + " ✓" * d.done, dep_html(d)) for d in tr.deps(t)]

    def unblocks_items(t: Record) -> list[tuple[str, str]]:
        return [(o.id, ref(o.id)) for o in tr.waiting_on(t.id)]

    def fit(items: list[tuple[str, str]]) -> tuple[int, str]:
        """How many items fit in LIST_CH characters with ` +n` for the rest (at least one), and the text shown."""
        texts = [text for text, _ in items]
        for k in range(len(texts), 0, -1):
            shown = ", ".join(texts[:k]) + (f" +{len(texts) - k}" if k < len(texts) else "")
            if len(shown) <= LIST_CH or k == 1:
                return k, shown
        return 0, ""

    def list_cell(items: list[tuple[str, str]]) -> str:
        k = fit(items)[0]
        more = f' <span class=meta>+{len(items) - k}</span>' if k < len(items) else ""
        return (f'<span class=deps title="{e(", ".join(text for text, _ in items))}">'
                f'{", ".join(h for _, h in items[:k])}{more}</span>')

    def ticket_row(t: Record, order: int) -> str:
        """A row of the sequence, which opens to the whole ticket. A ready ticket shows `ready` for its `todo`.
        Its data-* carry what the page sorts it by (viewer/app.js): `o` its place in the dependency order, `g` its
        group, `r` its status's place in STAGES (an unknown status after them, so the page still renders and shows the
        check's error), `p` its priority's rank (most urgent 0, none empty), `sw` and `sc` its wait and cycle times in
        seconds (none empty), `w` and `u` how many it waits on and unblocks."""
        closed = t.stage in CLOSED_TICKET
        tag, gate_chip = gate_html(t)
        cls = " closed" if closed else " s-stack" if tr.stackable(t) else " s-blocked" if tr.blockers(t) else ""
        title = str(t.get("title"))
        pr = f' <span class=meta>PR {e(pr_label(t))}</span>' if t.get("pr") else ""
        word, line = ("summary", t.get("summary")) if closed else ("next", t.get("next"))
        lead = (word, md_inline(str(line))) if line else None
        spans = {name: span(t, name) for name in SPANS}
        step = seq.step.get(t.id, "")
        cells = (f'<span>{step}</span>'
                 f'<span class="tk tone" title="{e(t.id)} {e(title)}"><span class=id>{e(t.id)}</span> '
                 f'{e(title)}{pr}</span>'
                 f'<span>{e(str(t.get("group", "")))}</span>'
                 f'<span>{gate_chip if tag == "ready" else chip(t.stage)}</span>'
                 f'<span>{e(str(t.get("priority", "")))}</span>'
                 + "".join(f'<span>{"" if x is None else duration(x)}</span>' for x in spans.values())
                 + f'{list_cell(waits_items(t))}{list_cell(unblocks_items(t))}')
        status = STAGES.index(t.stage) if t.stage in STAGES else len(STAGES)
        rank = priority_rank(str(t.get("priority", "")))
        sort = (f'data-o="{order}" data-g="{e(str(t.get("group", "")))}" data-r="{status}" '
                f'data-p="{"" if rank is None else rank}" '
                + "".join(f'data-s{name[0]}="{"" if x is None else x}" ' for name, x in spans.items())
                + f'data-w="{len(waits_items(t))}" '
                f'data-u="{len(unblocks_items(t))}"')
        return panel(t.id, cells, body_html(t, lead), attrs=f' class="t{cls}" data-s="{e(t.stage)}" '
                     f'data-c="{int(closed)}" data-b="{tag}" data-step="{step}" {sort}')

    def now_item(key: str, head: str, line: str, more: str = "", attrs: str = "") -> str:
        """A Now entry: its head and the start of its line; it opens to the whole line and `more`."""
        return panel(f"_now-{key}", head, (f"<p>{line}</p>" if line else "") + more, line, attrs)

    # Your move first, then the others' by who, then the tickets whose PR GitHub has not been read for; in progress
    # before in review within each.
    state = tr.state()
    marks, handoffs = state.get("synced", {}), state.get("handoff", {})
    one_repo = len(tr.repos) <= 1
    now = []
    moves = {t.id: whose_move(tr, t) for t in tr.tickets if t.stage in IN_FLIGHT}

    # The sessions `tracker start` tied to this tracker that run on this machine: an icon by each ticket they have
    # under way; a session with none gets its own entry.
    live = [(x, match_cwd(x.cwd, x.sid, tr)) for x in live_sessions(tr.slug)]
    agents = {i: [x for x, m in live if i in (r.id for r in m.active)] for i in moves}

    def agents_html(sessions: list[Live]) -> str:
        return "".join(f"<p class=meta>session {e(x.name)}: {e(agent_word(x))}, in {e(x.cwd)}</p>" for x in sessions)

    def turn(t: Record) -> tuple:
        move = moves[t.id]
        return (2 if not move else 0 if move.mine else 1, move.rank if move and move.mine else 0,
                move.who if move else "", t.stage != "in-progress")

    for t in sorted((t for t in tr.tickets if t.id in moves), key=turn):
        b = str(t.get("branch") or "")
        mark = branch_entry(marks, tr.repo_of(t), b)
        fresh = f"commits logged {ago(mark['at'])}" if isinstance(mark, dict) else ""
        stale = fresh if fresh and time.time() - mark["at"] > STALE_MARK_S else ""
        head = (agent_icon(agents[t.id]) + ref(t.id) + chip(t.stage) + move_chip(moves[t.id])
                + named(e(str(t.get("title"))), " · ".join(x for x in (e(b), stale) if x)))
        line = f'next: {md_inline(str(t.get("next")))}' if t.get("next") else ""
        mine = moves[t.id] and moves[t.id].mine
        now.append(now_item(t.id, head, line, (f"<p class=meta>{fresh}</p>" if fresh else "")
                            + agents_html(agents[t.id]), " class=mine" if mine else ""))
    for key, h in handoffs.items():
        b = key.rpartition(":")[2] if one_repo else key  # a tracker that spans repos names the repo
        where = f"at {e(h.get('head', '')[:9])}" + (", uncommitted changes" if h.get("dirty") else "")
        head = named(f"Handoff on {e(b)}", f'{ago(h.get("at", 0))}, {where}')
        now.append(now_item(f"handoff-{key}", head, md_inline(h.get("text", "")), attrs=" class=handoff"))
    busy = len(now)
    for x, m in live:
        if not m.active:
            open_ids = ", ".join(f"{ref(t.id)} {e(t.stage)}" for t in m.focus)
            head = agent_icon([x]) + named(e(x.name), e(x.branch))
            now.append(now_item(f"agent-{x.sid}", head, f"on {open_ids}" if open_ids else "on no ticket",
                                agents_html([x])))
    yours = sum(bool(m and m.mine) for m in moves.values())
    if not any(t.stage in IN_FLIGHT for t in tr.tickets) and tr.meta.get("status") == "active":
        ready = ", ".join(ref(t.id) for t in tr.ready())
        now.append(f'<p class=idle>Nothing in progress.{" Ready: " + ready if ready else ""}</p>')
    agents_n = f" · {len(live)} agent{'s' * (len(live) > 1)}" if live else ""
    w = watcher_of(tr)  # `tracker watch`: who watches the tracker for the user, or when the watch ended
    if w:
        text = f"Watched by {e(w['who'])} since {clock(w['since'])}" if w["running"] else \
            f"Watch by {e(w['who'])} ended {clock(w['ended'])}"
        now.append(f'<p class="watch{"" if w["running"] else " ended"}">{text}</p>')
    now_html = sec("now", "Now", f"{busy}" + (f" · {yours} your move" if yours else "") + agents_n,
                   f'<div class=nowlist>{"".join(now)}</div>', True) if now else ""

    # Dropped tickets go last. Columns are as wide as their longest text, so every row lines up.
    ordered = sorted(tr.tickets, key=lambda t: (t.stage == "dropped", seq.step.get(t.id, 0), sort_key(t.id)))

    def width(texts: list[str], least: int) -> int:
        return min(26, max([least, *(len(x) + 1 for x in texts)]))

    cols = (f"{width([str(t.get('group', '')) for t in ordered], 6)}ch 12ch "
            f"{width([str(t.get('priority', '')) for t in ordered], 9)}ch 10ch 10ch "
            f"{width([fit(waits_items(t))[1] for t in ordered], 10)}ch "
            f"{width([fit(unblocks_items(t))[1] for t in ordered], 10)}ch")
    n = {"all": len(tr.tickets), "active": sum(1 for t in tr.tickets if t.stage not in CLOSED_TICKET),
         "ready": len(tr.ready()),
         "blocked": sum(1 for t in tr.tickets if t.stage not in CLOSED_TICKET and tr.blockers(t)), **counts}
    filters = "".join(f'<button data-f="{f}">{f} <span class=n>{n[f]}</span></button>'
                      for f in ["all", "active", "ready", "blocked", *[s for s in STAGES if s in counts]])
    # Each heading sorts the rows by its column in the page; Step puts back the dependency order.
    head = "".join(f'<span><button type=button data-sort="{key}">{label}</button></span>'
                   for key, label in (("step", "Step"), ("ticket", "Ticket"), ("group", "Group"),
                                      ("status", "Status"), ("priority", "Priority"), ("wait", "Wait time"),
                                      ("cycle", "Cycle time"), ("waits", "Waits on"), ("unblocks", "Unblocks")))
    sequence_html = sec("seq", "Sequence", f"{n['active']} open of {n['all']}",
                        "".join(f"<p class=lead>{e(x)}</p>" for x in span_lines(tr))
                        + f'<div class=filters>{filters}</div><p class=sr-only aria-live=polite id=sort-said></p>'
                        f'<div class=seq style="--cols: {cols}"><div class=seq-head>{head}</div>'
                        + "".join(ticket_row(t, i) for i, t in enumerate(ordered)) + "</div>", True)

    open_ds = tr.open_decisions()
    open_d = "".join(decision_html(d) for d in open_ds) or "<p class=meta>None.</p>"
    settled = [d for d in tr.decisions if d.get("status") == "closed"]
    log_path = tr.root / "log.md"
    log_lines = [ln for ln in log_path.read_text().splitlines() if ln.startswith("- ")] if log_path.exists() else []
    log = md_to_html("\n".join(reversed(log_lines))) if log_lines else "<p class=meta>Empty.</p>"
    errors, warnings = check(tr)
    problems = "".join(f"<li>✗ {e(x)}</li>" for x in errors) + "".join(f"<li>⚠ {e(x)}</li>" for x in warnings)
    facts = " · ".join(f"{k}: {e(str(tr.meta[k]))}" for k in ("status", "owner", "repo", "created") if tr.meta.get(k))
    extra = tr.readme_body
    for h in README_SECTIONS:
        extra = without_section(extra, h)
    goal = section_block(tr.readme_body, "Goal") + section_block(tr.readme_body, "Scope")
    goal_html = panel("_goal", named(e(", ".join(headings(goal)))), md_to_html(goal)) if goal else ""

    # Reference: what is settled or past, one collapsed row each.
    reference = "".join([
        panel("_closed", named("Closed decisions", str(len(settled))), "".join(decision_html(d) for d in settled))
        if settled else "",
        panel("_readme", named("More about this work", e(", ".join(headings(extra)))), md_to_html(extra))
        if headings(extra) else "",
        panel("_log", named("Log", f"{len(log_lines)} entries, newest first"), log,
              md_inline(log_lines[-1][2:]) if log_lines else "")])
    labels = ", ".join(dict.fromkeys(x.label.lower() for x in tr.context))
    links_row = (panel("_links", named("Links", f"{len(tr.context)}: {e(labels)}"), props_html([link_rows(tr.context)]))
                 if tr.context else "")
    state_line = issue_state(tr)
    issue_html = f"<span hidden id=issue-state>{e(state_line)}</span>" if state_line else ""
    return f"""{issue_html}
<h1>{e(tr.title)}</h1><p class=sub>{facts}<br>{e(tr.slug)} · <code>{e(str(tr.root))}</code></p>
{links_row}
{goal_html}
{now_html}
{sequence_html}
{sec("check", "Check", len(errors) + len(warnings), f"<ul>{problems}</ul>") if problems else ""}
{sec("decisions", "Open decisions", len(open_ds), open_d, True)}
{sec("reference", "Reference", 0, reference)}"""


def issue_state(tr: Tracker) -> str:
    """What the page's live line says of the issue fields: when they were last read, or what a Refresh waits on.
    Empty when no ticket has an Issue link."""
    linked = [t for t in tr.tickets if t.aliases]
    if not linked:
        return ""
    issues = tr.raw_state().get("issues", {})
    read, asked = issues.get("read", {}), issues.get("requested", 0)
    waiting = asked and any(t.stage not in CLOSED_TICKET and read.get(t.id, 0) < asked for t in linked)
    if waiting:
        who = ("waiting for a session to read them" if live_sessions(tr.slug)
               else "no session on this tracker to read them")
        return f"issues: asked; {who}"
    last = max((read[t.id] for t in linked if read.get(t.id)), default=0)
    return f"issues read {clock(last)}" if last else "issues not read yet"


def request_refresh(tr: Tracker) -> None:
    """The page's Refresh: ask again for every open ticket's issue fields. Only the model can read an issue tracker
    (through its tool), so this records the request; the next prompt hook passes it on."""
    with locked():
        state = tr.raw_state()
        state.setdefault("issues", {})["requested"] = time.time()
        tr.save_state(state)


def page_html(title: str, body: str, slug: str = "", ver: str = "") -> str:
    """viewer/page.html with its placeholders filled; {{body}} is already HTML, the rest are escaped."""
    page = (VIEWER_DIR / "page.html").read_text()
    for key, value in (("title", title), ("slug", slug), ("version", ver), ("token", TOKEN)):
        page = page.replace("{{" + key + "}}", html.escape(value))
    return page.replace("{{body}}", body)


# ---------------------------------------------------------------- viewer server

VIEWER_FILE = HOME / ".viewer.json"
# Longer than Chrome's once-a-minute timer throttling in background tabs, so a hidden tab keeps it alive.
VIEWER_IDLE_S = int(os.environ.get("TRACKER_VIEWER_IDLE", "180"))


def code_id() -> str:
    """`work-tracker <hash of the package's modules> <package path>`: names the code a viewer runs."""
    digest = hashlib.sha1(b"".join(p.read_bytes() for p in sorted(PACKAGE.glob("*.py"))))
    return f"work-tracker {digest.hexdigest()[:12]} {PACKAGE}"


CODE_ID = code_id()
TOKEN = secrets.token_urlsafe(16)  # in each page; a Refresh must send it, which another site's page cannot read
REFRESH_SYNC_S = 15  # a Refresh syncs GitHub unless a sync ran this recently
VIEWER_SYNC_S = int(os.environ.get("TRACKER_VIEWER_SYNC", "120"))  # GitHub sync while a page is open
RESTART_WAIT_S = 10  # how long `open` waits for a viewer on this code to restart onto a new version of it


def viewer_ping() -> tuple[int, str, int] | None:
    """(port, ping answer, pid) of the viewer named in VIEWER_FILE, if it answers."""
    import urllib.request
    try:
        info = json.loads(VIEWER_FILE.read_text())
        port = int(info["port"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
            return port, r.read().decode(), int(info["pid"])
    except (OSError, ValueError, KeyError):
        return None


def viewer_port() -> int | None:
    """The port of a running viewer on this code, or None. A viewer on this package restarts itself when the code
    changes, so wait for it; a viewer on another copy of the plugin is stopped, so `open` starts one on this copy."""
    deadline = time.monotonic() + RESTART_WAIT_S
    while True:
        got = viewer_ping()
        if not got or not got[1].startswith("work-tracker"):
            return None
        port, answer, pid = got
        if answer == CODE_ID:
            return port
        if not answer.endswith(f" {PACKAGE}") or time.monotonic() > deadline:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            return None
        time.sleep(0.3)


_refused = ""  # a code id that did not import: tried again only once the code changes


def code_ready() -> bool:
    """The package on disk is a new version that imports in a fresh Python: safe to restart onto. A change saved one
    file at a time can compile before its last file is saved, and not import."""
    global _refused
    try:
        new = code_id()
    except OSError:
        return False
    if new in (CODE_ID, _refused):
        return False
    check = f"import sys; sys.path.insert(0, {str(PACKAGE.parent)!r}); import tracker.cli, tracker.viewer"
    try:
        ok = subprocess.run([*PYTHON, "-c", check], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        _refused = new
    return ok


def serve(port: int = 0) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            server.last_seen = time.monotonic()
            port = server.server_address[1]
            if (self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}")
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                return self.reply(403, "forbidden", "text/plain")  # DNS rebinding; a request another site's page sends
            parts = [p for p in self.path.split("?")[0].split("#")[0].split("/") if p]
            if parts == ["ping"]:
                return self.reply(200, CODE_ID, "text/plain")
            if len(parts) == 2 and parts[0] == "assets":
                asset = VIEWER_DIR / parts[1]
                if asset.suffix in ASSET_TYPES and asset.parent == VIEWER_DIR and asset.is_file():
                    return self.reply(200, asset.read_text(), ASSET_TYPES[asset.suffix])
                return self.reply(404, "not found", "text/plain")
            if not parts:
                items = "".join(f'<li><a href="/t/{html.escape(t.slug)}/">{html.escape(t.title)}</a> '
                                f'<span class=meta>{html.escape(t.slug)}</span></li>' for t in all_trackers())
                return self.reply(200, page_html("Trackers", f"<h1>Trackers</h1><ul>{items}</ul>"))
            tr = next((t for t in all_trackers() if parts[0] == "t" and len(parts) > 1 and t.slug == parts[1]), None)
            if not tr:
                return self.reply(404, "no such tracker", "text/plain")
            server.viewed[tr.slug] = time.monotonic()
            rest = parts[2:]
            if rest == ["version"]:
                return self.reply(200, version(tr), "text/plain")
            if rest == ["main"]:
                return self.reply(200, main_html(tr))
            if not rest:
                if not self.path.split("?")[0].endswith("/"):  # relative evidence/ links resolve under the slash
                    self.send_response(301)
                    self.send_header("Location", f"/t/{tr.slug}/")
                    self.end_headers()
                    return None
                return self.reply(200, page_html(tr.title, main_html(tr), tr.slug, version(tr)))
            if rest[0] == EVIDENCE_DIR and len(rest) > 1:
                return self.evidence(tr, rest[1:])
            return self.reply(404, "not found", "text/plain")

        def do_POST(self):
            """A Refresh: the page's token, from this server's own page, or nothing changes."""
            server.last_seen = time.monotonic()
            port = server.server_address[1]
            if (self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}")
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or not hmac.compare_digest(self.headers.get("X-Tracker-Token", ""), TOKEN)):
                return self.reply(403, "forbidden", "text/plain")
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            tr = next((t for t in all_trackers() if parts[:1] == ["t"] and len(parts) == 3 and t.slug == parts[1]),
                      None)
            if not tr or parts[2] != "refresh":
                return self.reply(404, "not found", "text/plain")
            request_refresh(tr)

            def pull():
                try:
                    sync(tr, force=False, min_interval=REFRESH_SYNC_S)
                except (Exception, SystemExit):  # gh missing or a busy lock: the next round tries again
                    pass
            threading.Thread(target=pull, daemon=True).start()
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return None

        def evidence(self, tr: Tracker, parts: list[str]) -> None:
            folder = (tr.root / EVIDENCE_DIR).resolve()
            path = folder.joinpath(*parts).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                return self.reply(404, "not found", "text/plain")
            if path.suffix == ".md":
                name = "/".join(parts)
                return self.reply(200, page_html(name, f"<p class=sub><a href=\"/t/{html.escape(tr.slug)}/\">"
                                                       f"{html.escape(tr.title)}</a> · {html.escape(name)}</p>"
                                                       + md_to_html(path.read_text(errors="replace"))))
            ctype = EVIDENCE_TYPES.get(path.suffix.lower())
            data = path.read_bytes()
            if not ctype:
                ctype = "text/plain; charset=utf-8" if b"\0" not in data[:4096] else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "sandbox")  # a file runs no script in the viewer's origin
            self.end_headers()
            self.wfile.write(data)

        def reply(self, code: int, body: str, ctype: str = "text/html") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # Only app.js runs: no inline script or handler, if tracker text ever gets past md_inline's escaping.
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    for _ in range(20):  # after a restart the old socket may take a moment to free the port
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            time.sleep(0.25)
    else:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.last_seen = time.monotonic()
    server.viewed = {}  # slug -> when a page last asked for it
    server.restart = False
    atomic_write(VIEWER_FILE, json.dumps({"port": server.server_address[1], "pid": os.getpid()}))

    def watchdog():
        """Stop when idle; restart onto this script when it changes."""
        while time.monotonic() - server.last_seen < VIEWER_IDLE_S:
            time.sleep(2)
            if code_ready():
                server.restart = True
                break
        server.shutdown()

    def syncer():
        """Pull PR state from GitHub for the trackers that open pages show, so their stages stay current."""
        while True:
            time.sleep(10)
            for slug, seen in list(server.viewed.items()):
                tr = tracker_at(slug) if time.monotonic() - seen < 60 else None
                if tr:
                    try:
                        sync(tr, force=False, min_interval=VIEWER_SYNC_S)
                    except (Exception, SystemExit):  # gh missing, a bad file, a busy lock: try again next round
                        pass

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=syncer, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if server.restart:
            port = str(server.server_address[1])
            if not WINDOWS:
                os.execv(sys.executable, [*CLI, "serve", "--port", port])
            spawn("serve", "--port", port)  # Windows has no exec: start the new viewer, and this one ends
        try:
            if json.loads(VIEWER_FILE.read_text()).get("pid") == os.getpid():
                VIEWER_FILE.unlink()
        except (OSError, ValueError):
            pass
