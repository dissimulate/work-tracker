"""The live viewer: the page's HTML, and the localhost server that serves it and restarts on new code."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import string
import subprocess
import sys
import threading
import time
from typing import Callable, NamedTuple

from .markdown import headings, section_block, strip_comments, without_section, Link
from .model import (CLI, CLOSED_TICKET, EVIDENCE_DIR, HOME, IN_FLIGHT, LIST_KEYS, PACKAGE, PYTHON, README_SECTIONS,
    ROOT, SIZE_NAMES, SPANS, STAGES, WINDOWS, all_trackers, archived_at, archived_trackers, atomic_write, branch_entry,
    close_action, due_date, files_hash, level, locked, sequence, sort_key, span, spawn, tracker_at, whose_move, Busy,
    Dep, Move, Record, Tracker)
from .session import ago, live_by_tracker, live_sessions, match_cwd, Live
from .contract import check
from .views import duration, pr_label, span_lines, stage_counts, start_text
from .github import sync
from .watcher import archive_tracker, delete_tracker, in_use, unarchive_tracker, watcher_of, Refused

# ---------------------------------------------------------------- render

class Html(str):
    """Markup for the page; a plain str is text. Html.format, Html.join and + escape the text they take and keep
    markup as is. An f-string or str.join gives a plain str, which the next of them escapes, and `"<p>" + Html`
    escapes the "<p>": build markup only with these, from Html literals."""

    def format(self, *args: object, **kwargs: object) -> Html:
        return Html(ESCAPING.vformat(self, args, kwargs))

    def join(self, parts) -> Html:
        return Html(str.join(self, map(esc, parts)))

    def __add__(self, other: str) -> Html:
        return Html(str.__add__(self, esc(other)))

    def __radd__(self, other: str) -> Html:
        return Html(str.__add__(esc(other), self))

    def __mul__(self, n: int) -> Html:
        return Html(str.__mul__(self, n))


class Escaping(string.Formatter):
    def format_field(self, value: object, spec: str) -> str:
        shown = format(value, spec)
        return shown if isinstance(value, Html) else html.escape(shown)


ESCAPING = Escaping()
NONE = Html("")


def esc(x: object) -> Html:
    """Text as markup: escaped, unless it is markup already."""
    return x if isinstance(x, Html) else Html(html.escape(str(x)))


def attributes(pairs: dict[str, object]) -> Html:
    """` name="value"` per pair; a None or False value leaves its attribute out, and True writes it bare."""
    return NONE.join(Html(" {}").format(k) if v is True else Html(' {}="{}"').format(k, v)
                     for k, v in pairs.items() if v is not None and v is not False)


# The Markdown subset escapes its text itself, and returns Html.
def md_inline(s: str) -> Html:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\s][^*]*)\*(?!\w)", r"<em>\1</em>", s)
    # One pass for both forms: a bare URL inside a link's URL must not become a second link, whose quotes would end
    # the first one's href and open its tag to new attributes.
    return Html(re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)|(?<![\"'>=])(https?://[^\s<)\"']+)",
                       lambda m: link_html(m[2], m[1]) if m[1] else link_html(m[3], m[3]), s))


SAFE_HREF = re.compile(rf"(?:https?://|mailto:|#|{EVIDENCE_DIR}/)", re.I)


def link_html(url: str, text: str) -> str:
    """A link for already-escaped text; a URL that is not http(s), mailto, an in-page anchor or an evidence/ file
    stays plain text."""
    if not SAFE_HREF.match(url):
        return text
    return f'<a href="{url.replace(chr(34), "&quot;")}">{text}</a>'


def md_to_html(md: str) -> Html:
    """A small Markdown subset: headings, lists (nested by indent), code fences, tables, paragraphs."""
    out, para, lines, i = [], [], strip_comments(md).splitlines(), 0

    def inline(text: str) -> str:  # plain str: `"<p>" + Html` would escape the "<p>"
        return str(md_inline(text))

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
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
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
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
                out.append("<li>" + inline(m.group(2)) + "</li>")
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
                out.append("<div class=tbl><table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>")
                out += ["<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body]
                out.append("</table></div>")
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return Html("\n".join(out))


VIEWER_DIR = ROOT / "viewer"
ASSET_TYPES = {".css": "text/css", ".js": "text/javascript"}
EVIDENCE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".svg": "image/svg+xml", ".pdf": "application/pdf", ".html": "text/plain; charset=utf-8"}


def props_html(groups: list[list[tuple[str, str]]]) -> Html:
    """Label and value rows in one grid, so the values line up; each group after the first starts with a gap."""
    rows = NONE.join(Html("<dt{}>{}</dt><dd>{}</dd>").format(Html(" class=gap") if i and not j else "", label, value)
                     for i, group in enumerate(g for g in groups if g) for j, (label, value) in enumerate(group))
    return Html("<dl class=props>{}</dl>").format(rows) if rows else NONE


def link_rows(items: list[Link]) -> list[tuple[str, Html]]:
    return [(x.label, md_inline(x.text) + NONE.join(Html("<br><small>{}</small>").format(md_inline(y)) for y in x.sub))
            for x in items]


def chip(tone: str, text: str = "", *, cls: str = "") -> Html:
    """A status tag; its tone maps to the shared status colours, and `cls` adds a layout variant."""
    return Html('<span class="chip{} s-{}">{}</span>').format(f" {cls}" if cls else "", tone, text or tone)


def move_chip(move: Move | None) -> Html:
    """Whose move a ticket under way waits on: yours stands out, another's does not."""
    return chip("you" if move.mine else "them", move.text(), cls="move") \
        if move else NONE


def agent_word(s: Live) -> str:
    """`working since 09:52`: an agent session's status, and since when (a clock time: the page does not change
    while the status holds)."""
    word = {"busy": "working"}.get(s.status, s.status or "running")
    return f"{word} since {clock(s.since)}" if s.since else word


def clock(ts: float) -> str:
    """`09:52` today, `Tue 09:52` on another day."""
    same_day = time.localtime(ts)[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if same_day else "%a %H:%M", time.localtime(ts))


def agent_icon(sessions: list[Live]) -> Html:
    """Left of a Now row, the agent sessions on its work: a ring that spins while one of them works (busy) and stays
    still while all are idle (they wait for the user)."""
    if not sessions:
        return NONE
    word = "working" if any(x.status == "busy" for x in sessions) else "idle"
    title = "; ".join(f"{x.name}: {agent_word(x)}" for x in sessions)
    return Html('<span class="agent{}" role="img" aria-label="agent {}" title="{}"></span>').format(
        " busy" * (word == "working"), word, title)


def version(tr: Tracker) -> str:
    """`<data>.<code>.<synced>`: the first part changes with any tracker file, the sessions running on it, its
    watcher or its archiving (the page swaps its content), the second with any viewer/ file or module of this package
    (the page reloads), the third is the last GitHub sync (epoch s)."""
    live = json.dumps([[x.sid, x.status, x.since, x.branch, x.focus] for x in live_sessions(tr.slug)]
                      + [watcher_of(tr), tr.archived])
    code = files_hash([*sorted(VIEWER_DIR.iterdir()), *sorted(PACKAGE.glob("*.py"))])
    return f"{files_hash(tr.data_files(), live)}.{code}.{int(tr.raw_state().get('last_sync', 0))}"


# ---------------------------------------------------------------- page parts

def panel(key: str, head: Html, body: Html, line: Html | str = NONE, attrs: dict[str, object] | None = None) -> Html:
    """Every part of the page that opens: its head, then `line` cut to one line while it is closed."""
    nx = Html("<span class=nx>{}</span>").format(line) if line else NONE
    return Html('<details data-id="{}"{}><summary>{}{}</summary><div>{}</div></details>').format(
        key, attributes(attrs or {}), head, nx, body)


def sec(key: str, title: str, count: int | str, body: Html, start_open: bool = False) -> Html:
    """A section: its heading opens and closes it. The count shows while it is closed."""
    return panel(f"_sec-{key}", Html("<h2>{}</h2>").format(f"{title} · {count}" if count else title), body,
                 attrs={"class": "sec", "open": start_open})


def named(name: str, meta: str = "") -> Html:
    return Html("<b>{}</b>").format(name) + (Html("<span class=meta>{}</span>").format(meta) if meta else NONE)


def ref(ident: str) -> Html:
    return Html('<a class=id href="#{0}">{0}</a>').format(ident)


def comma(parts) -> Html:
    return Html(", ").join(parts)


def toned(tone: str, body: Html | str, title: str | None = None) -> Html:
    """Text in a status colour (a dependency, a decision); a link or id in it takes the same colour."""
    return Html('<span class="tone s-{}"{}>{}</span>').format(tone, attributes({"title": title}), body)


def dep_html(d: Dep) -> Html:
    if not d.rec:
        return toned("blocked", d.ident, d.link.text if d.link else None)
    tone = "done" if d.done else "stack" if d.kind == "ticket" and d.rec.stage in IN_FLIGHT else "blocked"
    return toned(tone, ref(d.ident) + " ✓" * d.done)


def gate_html(tr: Tracker, r: Record) -> tuple[str, Html]:
    """(filter tag, summary chip): what a ticket waits on, or that it is ready; what an open decision blocks."""
    if r.kind == "decision":
        later = [t.id for t in tr.waiting_on(r.id)]
        return "", chip("blocked", f"blocks {', '.join(later)}") if later and r.get("status") == "open" else NONE
    if r.stage in CLOSED_TICKET:
        return "", NONE
    blockers = tr.blockers(r)
    if blockers:
        tone, verb = ("stack", "stacks on") if tr.stackable(r) else ("blocked", "waits on")
        return "blocked", chip(tone, f"{verb} {', '.join(d.ident for d in blockers)}")
    return ("ready", chip("ready")) if r.stage == "todo" else ("", NONE)


def body_html(tr: Tracker, r: Record, lead: tuple[str, Html] | None = None) -> Html:
    """What a ticket or decision shows when it opens: one grid of its line (next or summary), dependencies, facts and
    links, then the body."""
    deps = [("waits on", comma(dep_html(d) for d in tr.deps(r)))] if r.kind == "ticket" and tr.deps(r) else []
    start = tr.start_point(r) if r.kind == "ticket" else None
    if start and start.stacked:
        deps.append(("start", start_text(tr, start)))
    if tr.waiting_on(r.id):
        deps.append(("unblocks" if r.kind == "ticket" else "blocks", comma(ref(t.id) for t in tr.waiting_on(r.id))))
    if r.kind == "ticket" and tr.decisions_for(r):
        deps.append(("decisions", comma(
            toned("closed", ref(d.id) + " ✓") if d.get("status") == "closed" else toned("blocked", ref(d.id))
            for d in tr.decisions_for(r))))
    facts = []
    for k in ("branch", "base", "repo", "group", "refs", "owner", "started_at", "merged_at", "updated"):
        if r.get(k):
            value = (comma(ref(x) for x in r.list(k)) if k == "refs"
                     else ", ".join(r.list(k)) if k in LIST_KEYS else str(r.get(k)))
            facts.append((k.removesuffix("_at"),
                          Html("<code>{}</code>").format(value) if k in ("branch", "base") else value))
    move = whose_move(tr, r) if r.kind == "ticket" else None
    head = ([lead] if lead else []) + ([("move", move_chip(move))] if move else [])
    props = props_html([head, deps, facts, link_rows(r.links)])
    return Html("<div>{}{}</div>").format(props, md_to_html(without_section(r.body, "Links")))


def decision_html(tr: Tracker, d: Record) -> Html:
    return panel(d.id, Html("<span class=id>{}</span>{}{}<b>{}</b>").format(
        d.id, chip(d.get("status", "open")), gate_html(tr, d)[1], str(d.get("title"))), body_html(tr, d))


# ---------------------------------------------------------------- the sequence

LIST_CH = 24  # a list column shows the items that fit, then `+n`


class Row(NamedTuple):
    """A ticket of the sequence, with what its row shows worked out once. `waits` and `unblocks` are its two
    dependency lists, as (text, html) per item; a closed ticket's waits-on is history, which its row leaves out (the
    opened ticket still lists it)."""
    t: Record
    order: int  # its place in the dependency order the page starts in
    step: int | str
    links: list[str]  # the tickets it waits on that the graph draws (drawn_links)
    gate: tuple[str, Html]  # gate_html
    waits: list[tuple[str, Html]]
    unblocks: list[tuple[str, Html]]
    spans: dict[str, int | None]  # SPANS, in seconds

    @classmethod
    def of(cls, tr: Tracker, t: Record, order: int, step: int | str, links: list[str]) -> Row:
        waits = [] if t.stage in CLOSED_TICKET else [(d.ident + " ✓" * d.done, dep_html(d)) for d in tr.deps(t)]
        return cls(t, order, step, links, gate_html(tr, t), waits,
                   [(o.id, ref(o.id)) for o in tr.waiting_on(t.id)], {name: span(t, name) for name in SPANS})


class Cell(NamedTuple):
    """A cell of the sequence: its content and attributes."""
    body: Html | str
    attrs: dict[str, object] = {}


def fit(items: list[tuple[str, Html]], limit: int) -> int:
    """How many items fit in `limit` characters with ` +n` for the rest (at least one)."""
    texts = [text for text, _ in items]
    for k in range(len(texts), 0, -1):
        shown = ", ".join(texts[:k]) + (f" +{len(texts) - k}" if k < len(texts) else "")
        if len(shown) <= limit or k == 1:
            return k
    return 0


def step_cell(r: Row) -> Cell:
    """The graph's place: viewer/app.js draws the dependency graph over the column, from the rows' data."""
    return Cell(NONE, {"class": "graph", "title": f"step {r.step}"})


def ticket_cell(r: Row) -> Cell:
    pr = Html(" <span class=meta>PR {}</span>").format(pr_label(r.t)) if r.t.get("pr") else NONE
    title = str(r.t.get("title"))
    return Cell(Html("<span class=id>{}</span> {}{}").format(r.t.id, title, pr),
                {"class": "tk", "title": f"{r.t.id} {title}"})


def text_cell(key: str) -> Callable[[Row], Cell]:
    return lambda r: Cell(str(r.t.get(key, "")))


def priority_cell(r: Row) -> Cell:
    """P0 (most urgent) to P4; a value `check` refuses shows as it is."""
    n = level(r.t, "priority")
    return Cell(f"P{n}" if n is not None else str(r.t.get("priority", "")))


def size_cell(r: Row) -> Cell:
    """XS to XL; a value `check` refuses shows as it is."""
    n = level(r.t, "size")
    return Cell(SIZE_NAMES[n] if n is not None else str(r.t.get("size", "")))


def status_cell(r: Row) -> Cell:
    """A ready ticket shows `ready` for its `todo`."""
    tag, gate_chip = r.gate
    return Cell(gate_chip if tag == "ready" else chip(r.t.stage))


def time_cell(r: Row) -> Cell:
    """`wait → cycle`, either side blank when the ticket has none."""
    if all(x is None for x in r.spans.values()):
        return Cell("")
    shown = ["" if x is None else duration(x) for x in r.spans.values()]
    title = "; ".join(f"{name} ({SPANS[name][2]}): {text or 'none'}" for name, text in zip(r.spans, shown))
    return Cell(Html("{} <span class=meta>→</span> {}").format(*shown), {"title": title})


def deps_cell(r: Row) -> Cell:
    """A line `← ` what the ticket waits on, a line `→ ` what it unblocks."""
    lists = [(arrow, label, items) for arrow, label, items in
             (("←", "waits on", r.waits), ("→", "unblocks", r.unblocks)) if items]
    lines = []
    for arrow, _, items in lists:
        k = fit(items, LIST_CH - 2)
        more = Html(" <span class=meta>+{}</span>").format(len(items) - k) if k < len(items) else NONE
        lines.append(Html("<span><span class=meta>{}</span> {}{}</span>").format(
            arrow, comma(h for _, h in items[:k]), more))
    title = "; ".join(f"{label}: {', '.join(text for text, _ in items)}" for _, label, items in lists)
    return Cell(NONE.join(lines), {"class": "deps lines", "title": title or None})


class Column(NamedTuple):
    """A column of the sequence. `sorts`: a heading button per (key, label, what the page says it sorts by, value per
    row); a row carries each value as data-sort-<key>, which viewer/app.js sorts by, None or "" last. `width`: a CSS
    grid track shared by the header and rows. `hide`: the sequence width (HIDES) it leaves from; "" to always show."""
    sorts: tuple[tuple[str, str, str, Callable[[Row], object]], ...]
    cell: Callable[[Row], Cell]
    width: str
    hide: str = ""


HIDES = ("mid", "narrow")  # the sequence widths a column can leave from, widest first (viewer/style.css)

# An unknown status sorts after STAGES, so the page still renders and shows the check's error; a priority most urgent
# first; a size smallest first; dependencies by count.
COLUMNS = (
    Column((("step", "Step", "dependency order", lambda r: r.order),), step_cell, "max-content"),
    Column((("ticket", "Ticket", "ticket", lambda r: r.t.id),), ticket_cell, "minmax(0, 1fr)"),
    Column((("group", "Group", "group", lambda r: r.t.get("group", "")),),
           text_cell("group"), "fit-content(var(--meta-max))", "mid"),
    Column((("status", "Status", "status",
             lambda r: STAGES.index(r.t.stage) if r.t.stage in STAGES else len(STAGES)),), status_cell, "max-content"),
    Column((("priority", "Priority", "priority", lambda r: level(r.t, "priority")),), priority_cell, "max-content",
           "narrow"),
    Column((("size", "Size", "size", lambda r: level(r.t, "size")),), size_cell, "max-content", "narrow"),
    Column((("wait", "Wait", "wait time", lambda r: r.spans["wait"]),
            ("cycle", "→ Cycle", "cycle time", lambda r: r.spans["cycle"])), time_cell, "max-content", "mid"),
    Column((("waits", "← Waits on", "waits on", lambda r: len(r.waits)),
            ("unblocks", "→ Unblocks", "unblocks", lambda r: len(r.unblocks))),
           deps_cell, "fit-content(var(--meta-max))", "narrow"),
)


def hide_class(c: Column, cls: object = None) -> str | None:
    """A cell's class, with the column's hide-<width> when it has one."""
    return " ".join(x for x in (str(cls or ""), c.hide and f"hide-{c.hide}") if x) or None


def ticket_row(tr: Tracker, r: Row, cells: list[Cell]) -> Html:
    """A row of the sequence, which opens to the whole ticket. Its data-* carry its status (data-s), whether the server
    counts it closed (data-c), ready or blocked (data-b), the tickets it waits on and unblocks (data-waits,
    data-unblocks), its step (data-step) and the links the graph draws (data-links), from which viewer/app.js draws
    the graph and marks a hovered row's links, and its sort values (Column)."""
    t = r.t
    closed = t.stage in CLOSED_TICKET
    cls = " closed" if closed else " s-stack" if tr.stackable(t) else " s-blocked" if r.gate[0] == "blocked" else ""
    word, line = ("summary", t.get("summary")) if closed else ("next", t.get("next"))
    summary = NONE.join(Html("<span{}>{}</span>").format(attributes(
        {"class": hide_class(c, x.attrs.get("class")), **{k: v for k, v in x.attrs.items() if k != "class"}}), x.body)
        for c, x in zip(COLUMNS, cells))
    sorts = {f"data-sort-{key}": "" if (v := value(r)) is None else v
             for c in COLUMNS for key, _, _, value in c.sorts}
    waits = " ".join(d.ident for d in tr.deps(t) if d.kind == "ticket")
    unblocks = " ".join(o.id for o in tr.waiting_on(t.id) if o.kind == "ticket")
    attrs = {"class": f"t{cls}", "data-s": t.stage, "data-c": int(closed), "data-b": r.gate[0],
             "data-waits": waits or None, "data-unblocks": unblocks or None, "data-step": r.step,
             "data-links": " ".join(r.links) or None, **sorts}
    return panel(t.id, summary, body_html(tr, t, (word, md_inline(str(line))) if line else None), attrs=attrs)


def ticket_edges(tr: Tracker) -> dict[str, list[str]]:
    """Per ticket, the tickets it waits on: its depends_on, and those its open PR stacks it on."""
    return {t.id: list(dict.fromkeys(d.ident for d in tr.deps(t) if d.kind == "ticket" and d.ident != t.id))
            for t in tr.tickets}


def drawn_links(edges: dict[str, list[str]]) -> dict[str, list[str]]:
    """Per ticket, the tickets it waits on that no longer chain of waits implies (a transitive reduction): the graph
    draws only these, and the Deps column and a hover still show every one."""
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
    those ready. Dropped tickets, and any in a cycle, go last in step order."""
    live = {t.id for t in tr.tickets if t.stage != "dropped"}
    left = {i: sum(j in live for j in edges[i]) for i in live}
    after: dict[str, list[str]] = {i: [] for i in live}
    for i in live:
        for j in edges[i]:
            if j in live:
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
    return sorted(tr.tickets, key=lambda t: (t.id not in place, place.get(t.id, 0), t.stage == "dropped",
                                             step.get(t.id, 0), sort_key(t.id)))


def sequence_html(tr: Tracker) -> Html:
    """The tickets in depth-first dependency order (graph_order), under their filters and sortable column heads."""
    seq = sequence(tr)
    edges = ticket_edges(tr)
    links = drawn_links(edges)
    rows = [Row.of(tr, t, i, seq.step.get(t.id, ""), links[t.id])
            for i, t in enumerate(graph_order(tr, edges, seq.step))]
    cells = [[c.cell(r) for c in COLUMNS] for r in rows]

    def shown_at(c: Column, level: str) -> bool:
        return not c.hide or HIDES.index(c.hide) > HIDES.index(level)

    widths = [c.width for c in COLUMNS]
    style = "; ".join([f"--cols: {' '.join(widths)}"] + [
        f"--cols-{level}: {' '.join(w for c, w in zip(COLUMNS, widths) if shown_at(c, level))}" for level in HIDES])

    counts = stage_counts(tr)
    n = {"all": len(tr.tickets), "active": sum(1 for t in tr.tickets if t.stage not in CLOSED_TICKET),
         "ready": len(tr.ready()),
         "blocked": sum(1 for t in tr.tickets if t.stage not in CLOSED_TICKET and tr.blockers(t)), **counts}
    filters = NONE.join(Html('<button data-f="{0}">{0} <span class=n>{1}</span></button>').format(f, n[f])
                        for f in ["all", "active", "ready", "blocked", *[s for s in STAGES if s in counts]])
    # Each heading sorts the rows by its column in the page; Step puts back the dependency order.
    head = NONE.join(Html("<span{}>{}</span>").format(attributes({"class": hide_class(c)}), Html(" ").join(
        Html('<button type=button data-sort="{}" data-said="{}">{}</button>').format(key, said, label)
        for key, label, said, _ in c.sorts)) for c in COLUMNS)
    return sec("seq", "Sequence", f"{n['active']} open of {n['all']}",
               NONE.join(Html("<p class=lead>{}</p>").format(x) for x in span_lines(tr))
               + Html('<div class=filters>{}</div><p class=sr-only aria-live=polite id=sort-said></p>'
                      '<div class=seq-wrap><div class=seq style="{}">'
                      '<div class=seq-head>{}</div>{}</div></div>').format(
                   filters, style, head, NONE.join(ticket_row(tr, r, c) for r, c in zip(rows, cells))), True)


# ---------------------------------------------------------------- now

STALE_MARK_S = 86400  # a branch whose commits were last logged longer ago than this shows that age in its closed row


def now_html(tr: Tracker) -> Html:
    """Work under way (your move first, then the others' by who, then the tickets whose PR GitHub has not been read
    for; in progress before in review within each), each branch's handoff, the agent sessions on no ticket, and the
    watch."""

    def item(key: str, head: Html, line: Html | str, more: Html = NONE, attrs: dict[str, object] | None = None) -> Html:
        """An entry: its head and the start of its line; it opens to the whole line and `more`."""
        return panel(f"_now-{key}", head, (Html("<p>{}</p>").format(line) if line else NONE) + more, line, attrs)

    def agents_html(sessions: list[Live]) -> Html:
        return NONE.join(Html("<p class=meta>session {}: {}, in {}</p>").format(x.name, agent_word(x), x.cwd)
                         for x in sessions)

    state = tr.state()
    marks, handoffs = state.get("synced", {}), state.get("handoff", {})
    one_repo = len(tr.repos) <= 1
    now = []
    moves = {t.id: whose_move(tr, t) for t in tr.tickets if t.stage in IN_FLIGHT}

    # The sessions `tracker start` tied to this tracker that run on this machine: an icon by each ticket they have
    # under way; a session with none gets its own entry.
    live = [(x, match_cwd(x.cwd, x.sid, tr)) for x in live_sessions(tr.slug)]
    agents = {i: [x for x, m in live if i in (r.id for r in m.active)] for i in moves}

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
                + named(str(t.get("title")), " · ".join(x for x in (b, stale) if x)))
        line = Html("next: {}").format(md_inline(str(t.get("next")))) if t.get("next") else NONE
        now.append(item(t.id, head, line, (Html("<p class=meta>{}</p>").format(fresh) if fresh else NONE)
                        + agents_html(agents[t.id])))
    for key, h in handoffs.items():
        b = key.rpartition(":")[2] if one_repo else key  # a tracker that spans repos names the repo
        where = f"at {h.get('head', '')[:9]}" + (", uncommitted changes" if h.get("dirty") else "")
        head = named(f"Handoff on {b}", f'{ago(h.get("at", 0))}, {where}')
        now.append(item(f"handoff-{key}", head, md_inline(h.get("text", "")), attrs={"class": "handoff"}))
    busy = len(now)
    for x, m in live:
        if not m.active:
            open_ids = comma(ref(t.id) + f" {t.stage}" for t in m.focus)
            head = agent_icon([x]) + named(x.name, x.branch)
            now.append(item(f"agent-{x.sid}", head, Html("on {}").format(open_ids) if open_ids else "on no ticket",
                            agents_html([x])))
    yours = sum(bool(m and m.mine) for m in moves.values())
    if not any(t.stage in IN_FLIGHT for t in tr.tickets) and tr.meta.get("status") == "active":
        ready = comma(ref(t.id) for t in tr.ready())
        now.append(Html("<p class=idle>Nothing in progress.{}</p>").format(" Ready: " + ready if ready else ""))
    agents_n = f" · {len(live)} agent{'s' * (len(live) > 1)}" if live else ""
    w = watcher_of(tr)  # `tracker watch`: who watches the tracker for the user, or when the watch ended
    if w:
        text = f"Watched by {w['who']} since {clock(w['since'])}" if w["running"] else \
            f"Watch by {w['who']} ended {clock(w['ended'])}"
        now.append(Html('<p class="watch{}">{}</p>').format("" if w["running"] else " ended", text))
    return sec("now", "Now", f"{busy}" + (f" · {yours} your move" if yours else "") + agents_n,
               Html("<div class=nowlist>{}</div>").format(NONE.join(now)), True) if now else NONE


# ---------------------------------------------------------------- actions

CLOSE_ACTION = {"done": "done", "drop": "dropped"}  # a page's request: the status it sets


def due_text(d: dt.date) -> str:
    """`today`, `tomorrow`, a weekday within the week ahead, else `14 Oct` (with the year when not this one)."""
    days = (d - dt.date.today()).days
    if days in (0, 1):
        return ("today", "tomorrow")[days]
    if 1 < days < 7:
        return f"{d:%a}"
    return f"{d.day} {d:%b}" + (f" {d.year}" if d.year != dt.date.today().year else "")


def action_html(a: Record, buttons: bool) -> Html:
    """An action as a decision shows: one line that opens to what it concerns, its dates and its notes. The line: its
    id, its title (whole on hover), and when open its due day, if it has one (in the blocked colour once past, the
    active one on the day), with `buttons` Done and Drop; when closed, its status and when it closed."""
    notes = strip_comments(a.body).strip()
    status = str(a.get("status", "open"))
    due = due_date(a)
    if status != "open":
        when = Html("<span class=meta>{}</span>").format(a.get("updated"))
    elif due:
        late = " overdue" if due < dt.date.today() else " due-today" if due == dt.date.today() else ""
        when = Html('<span class="meta{}" title="{}">Due: {}</span>').format(late, due.isoformat(), due_text(due))
    else:
        when = NONE
    btns = Html("<span class=btns>{}</span>").format(NONE.join(
        Html('<button type=button data-close="{}" data-ref="{}">{}</button>').format(what, a.id, what.capitalize())
        for what in CLOSE_ACTION)) if buttons and status == "open" else NONE
    title = str(a.get("title"))
    head = Html('<span class=id>{}</span>{}<b title="{}">{}</b>{}{}').format(
        a.id, chip(status) if status != "open" else NONE, title, title, when, btns)
    facts = [(k, v) for k, v in (("concerns", comma(ref(x) for x in a.list("refs"))), ("due", a.get("due")),
                                 ("added", a.get("opened")), ("closed", a.get("updated") if status != "open" else ""))
             if v]
    return panel(a.id, head, Html("<div>{}{}</div>").format(props_html([facts]), md_to_html(notes) if notes else NONE),
                 attrs={"class": "act"})


def actions_html(tr: Tracker) -> Html:
    """The user's open actions, above Now so they are seen first; none open, no section."""
    acts = tr.open_actions()
    return sec("actions", "Your actions", len(acts), NONE.join(action_html(a, not tr.archived) for a in acts),
               True) if acts else NONE


def close_from_page(tr: Tracker, ident: str, what: str) -> str:
    """A page's Done or Drop: close the action, unless it is closed already. Returns why not, or ""."""
    with locked():
        tr = Tracker(tr.root)
        a = tr.action(ident)
        if not a:
            return f"no action {ident}"
        if a.get("status", "open") != "open":
            return f"{a.id} is {a.get('status')} already"
        close_action(tr, a, CLOSE_ACTION[what])
    return ""


# ---------------------------------------------------------------- the page

def reference_html(tr: Tracker) -> Html:
    """What is settled or past, one collapsed row each: closed decisions and actions, the rest of the README, the
    log."""
    settled = [d for d in tr.decisions if d.get("status") == "closed"]
    closed = sorted((a for a in tr.actions if a.get("status", "open") != "open"),
                    key=lambda a: (str(a.get("updated")), sort_key(a.id)), reverse=True)
    extra = tr.readme_body
    for h in README_SECTIONS:
        extra = without_section(extra, h)
    log_path = tr.root / "log.md"
    log_lines = [ln for ln in log_path.read_text().splitlines() if ln.startswith("- ")] if log_path.exists() else []
    log = md_to_html("\n".join(reversed(log_lines))) if log_lines else Html("<p class=meta>Empty.</p>")
    return sec("reference", "Reference", 0, NONE.join([
        panel("_closed", named("Closed decisions", str(len(settled))), NONE.join(decision_html(tr, d) for d in settled))
        if settled else NONE,
        panel("_closed-actions", named("Closed actions", f"{len(closed)}, newest first"),
              NONE.join(action_html(a, False) for a in closed)) if closed else NONE,
        panel("_readme", named("More about this work", ", ".join(headings(extra))), md_to_html(extra))
        if headings(extra) else NONE,
        panel("_log", named("Log", f"{len(log_lines)} entries, newest first"), log,
              md_inline(log_lines[-1][2:]) if log_lines else NONE)]))


def main_html(tr: Tracker) -> Html:
    errors, warnings = check(tr)
    problems = NONE.join([*(Html("<li>✗ {}</li>").format(x) for x in errors),
                          *(Html("<li>⚠ {}</li>").format(x) for x in warnings)])
    open_ds = tr.open_decisions()
    facts = " · ".join(f"{k}: {tr.meta[k]}" for k in ("status", "owner", "repo", "created") if tr.meta.get(k))
    goal = section_block(tr.readme_body, "Goal") + section_block(tr.readme_body, "Scope")
    labels = ", ".join(dict.fromkeys(x.label.lower() for x in tr.context))
    state_line = issue_state(tr)
    return Html("""{issue}{archived}
<h1>{title}</h1><p class=sub>{facts}<br>{slug} · <code>{root}</code></p>
{links}
{goal}
{actions}
{now}
{sequence}
{check}
{decisions}
{reference}""").format(
        issue=Html("<span hidden id=issue-state>{}</span>").format(state_line) if state_line else NONE,
        archived=Html('<p id=archived>Archived: out of every list and hook, never synced with GitHub. '
                      '<button type=button data-act=unarchive>Unarchive</button></p>') if tr.archived else NONE,
        title=tr.title, facts=facts, slug=tr.slug, root=str(tr.root),
        links=panel("_links", named("Links", f"{len(tr.context)}: {labels}"), props_html([link_rows(tr.context)]))
        if tr.context else NONE,
        goal=panel("_goal", named(", ".join(headings(goal))), md_to_html(goal)) if goal else NONE,
        actions=actions_html(tr), now=now_html(tr), sequence=sequence_html(tr),
        check=sec("check", "Check", len(errors) + len(warnings), Html("<ul>{}</ul>").format(problems))
        if problems else NONE,
        decisions=sec("decisions", "Open decisions", len(open_ds),
                      NONE.join(decision_html(tr, d) for d in open_ds) or Html("<p class=meta>None.</p>"), True),
        reference=reference_html(tr))


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


def page_html(title: str, body: Html, slug: str = "", ver: str = "") -> str:
    """viewer/page.html with its placeholders filled; {{body}} is already HTML, the rest are escaped."""
    page = (VIEWER_DIR / "page.html").read_text()
    for key, value in (("title", title), ("slug", slug), ("version", ver), ("token", TOKEN)):
        page = page.replace("{{" + key + "}}", html.escape(value))
    return page.replace("{{body}}", body)


# ---------------------------------------------------------------- viewer server

VIEWER_FILE = HOME / ".viewer.json"
# Longer than a browser's once-a-minute timer throttling in background tabs, so a hidden tab keeps it alive.
VIEWER_IDLE_S = int(os.environ.get("TRACKER_VIEWER_IDLE", "180"))
# One port, so a bookmark or a page left open works again once a viewer runs. 0: a free port each start, and the hooks
# start no viewer.
VIEWER_PORT = int(os.environ.get("TRACKER_VIEWER_PORT", "7316"))
SESSION_CHECK_S = 30  # how often an idle viewer looks for a tracked agent session that keeps it running


def code_id() -> str:
    """`work-tracker <hash of the package's modules> <package path>`: names the code a viewer runs."""
    digest = hashlib.sha1(b"".join(p.read_bytes() for p in sorted(PACKAGE.glob("*.py"))))
    return f"work-tracker {digest.hexdigest()[:12]} {PACKAGE}"


CODE_ID = code_id()
TOKEN = secrets.token_urlsafe(16)  # in each page; a Refresh must send it, which another site's page cannot read
REFRESH_SYNC_S = 15  # a Refresh syncs GitHub unless a sync ran this recently
VIEWER_SYNC_S = int(os.environ.get("TRACKER_VIEWER_SYNC", "120"))  # GitHub sync while a page is open
# Only same-origin scripts run: no inline script or handler, if tracker text gets past Html's escaping.
PAGE_CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'"
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


def keep_viewer() -> None:
    """From a tracked session's hooks: start the viewer when none runs, so the user's pages and bookmarks work while
    they work with an agent. A connect only, no HTTP: the hook stays fast."""
    import socket
    if not VIEWER_PORT:
        return
    try:
        socket.create_connection(("127.0.0.1", int(json.loads(VIEWER_FILE.read_text())["port"])), timeout=0.5).close()
    except (OSError, ValueError, KeyError, TypeError):
        spawn("serve", "--port", str(VIEWER_PORT))


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
        def allowed(self) -> bool:
            """A request to this server by its own name, not from another site's page (DNS rebinding, a cross-site
            request)."""
            server.last_seen = time.monotonic()
            port = server.server_address[1]
            return (self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")
                    and self.headers.get("Sec-Fetch-Site") != "cross-site")

        def parts(self) -> list[str]:
            return [p for p in self.path.split("?")[0].split("#")[0].split("/") if p]

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, "forbidden", "text/plain")
            parts = self.parts()
            if parts == ["ping"]:
                return self.reply(200, CODE_ID, "text/plain")
            if parts == ["trackers"]:  # the tracker menu: each tracker, and what keeps it from the archive or trash
                live = live_by_tracker()
                return self.reply(200, json.dumps({
                    "active": [{"slug": t.slug, "title": t.title, "root": str(t.root), "in_use": in_use(t, live=live)}
                               for t in all_trackers()],
                    "archived": [{"slug": t.slug, "title": t.title, "root": str(t.root)}
                                 for t in archived_trackers()]}),
                    "application/json")
            if len(parts) == 2 and parts[0] == "assets":
                asset = VIEWER_DIR / parts[1]
                if asset.suffix in ASSET_TYPES and asset.parent == VIEWER_DIR and asset.is_file():
                    return self.reply(200, asset.read_text(), ASSET_TYPES[asset.suffix])
                return self.reply(404, "not found", "text/plain")
            if not parts:
                def items(trackers: list[Tracker]) -> Html:
                    return NONE.join(Html('<li><a href="/t/{0}/">{1}</a> <span class=meta>{0}</span></li>').format(
                        t.slug, t.title) for t in trackers)
                archived = archived_trackers()
                return self.reply(200, page_html("Trackers", Html("<h1>Trackers</h1><ul>{}</ul>{}").format(
                    items(all_trackers()),
                    Html("<h2>Archived</h2><ul>{}</ul>").format(items(archived)) if archived else NONE)))
            tr = self.tracker(parts)
            if not tr:
                return self.reply(404, "no such tracker", "text/plain")
            if not tr.archived:  # the syncer's: an archived tracker is never synced
                server.viewed[tr.slug] = time.monotonic()
            rest = parts[2:]
            if rest == ["version"]:
                return self.reply(200, version(tr), "text/plain")
            if rest == ["main"]:
                return self.reply(200, main_html(tr))
            if not rest:
                if not self.path.split("?")[0].endswith("/"):  # relative evidence/ links resolve under the slash
                    return self.send(301, headers={"Location": f"/t/{tr.slug}/"})
                return self.reply(200, page_html(tr.title, main_html(tr), tr.slug, version(tr)))
            if rest[0] == EVIDENCE_DIR and len(rest) > 1:
                return self.evidence(tr, rest[1:])
            return self.reply(404, "not found", "text/plain")

        def tracker(self, parts: list[str]) -> Tracker | None:
            """The tracker `/t/<slug>/` names, archived or not: a slug names one tracker."""
            return tracker_at(parts[1]) or archived_at(parts[1]) if parts[0] == "t" and len(parts) > 1 else None

        def do_POST(self):
            """A Refresh, an Archive, an Unarchive, a Delete, or an action's Done or Drop
            (`/t/<slug>/actions/<A-n>/done|drop`): the page's token, from this server's own page, or nothing
            changes."""
            if not self.allowed() or not hmac.compare_digest(self.headers.get("X-Tracker-Token", ""), TOKEN):
                return self.reply(403, "forbidden", "text/plain")
            parts = self.parts()
            if len(parts) == 5 and parts[2] == "actions" and parts[4] in CLOSE_ACTION:
                tr = self.tracker(parts)
                if not tr or tr.archived:
                    return self.reply(404, "not found", "text/plain")
                try:
                    refused = close_from_page(tr, parts[3], parts[4])
                except Busy as exc:
                    refused = str(exc)
                return self.reply(409, refused, "text/plain") if refused else self.send(204)
            tr = self.tracker(parts) if len(parts) == 3 else None
            act = {"archive": archive_tracker, "unarchive": unarchive_tracker, "delete": delete_tracker}.get(
                parts[2] if tr else "")
            if not tr or not act and (parts[2] != "refresh" or tr.archived):
                return self.reply(404, "not found", "text/plain")
            if act:
                try:
                    act(tr)
                except (Refused, Busy) as exc:
                    return self.reply(409, str(exc), "text/plain")
                except OSError as exc:  # the folder stays where it was
                    return self.reply(500, f"not done: {exc}", "text/plain")
                server.viewed.pop(tr.slug, None)
                return self.send(204)
            request_refresh(tr)

            def pull():
                try:
                    sync(tr, force=False, min_interval=REFRESH_SYNC_S)
                except (Exception, SystemExit):  # gh missing or a busy lock: the next round tries again
                    pass
            threading.Thread(target=pull, daemon=True).start()
            return self.send(204)

        def evidence(self, tr: Tracker, parts: list[str]) -> None:
            folder = (tr.root / EVIDENCE_DIR).resolve()
            path = folder.joinpath(*parts).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                return self.reply(404, "not found", "text/plain")
            if path.suffix == ".md":
                name = "/".join(parts)
                return self.reply(200, page_html(name, Html('<p class=sub><a href="/t/{}/">{}</a> · {}</p>').format(
                    tr.slug, tr.title, name) + md_to_html(path.read_text(errors="replace"))))
            ctype = EVIDENCE_TYPES.get(path.suffix.lower())
            data = path.read_bytes()
            if not ctype:
                ctype = "text/plain; charset=utf-8" if b"\0" not in data[:4096] else "application/octet-stream"
            # A file runs no script in the viewer's origin.
            return self.send(200, data, {"Content-Type": ctype, "Content-Security-Policy": "sandbox"})

        def reply(self, code: int, body: str, ctype: str = "text/html") -> None:
            return self.send(code, body.encode(), {"Content-Type": f"{ctype}; charset=utf-8",
                                                   "Content-Security-Policy": PAGE_CSP})

        def send(self, code: int, data: bytes = b"", headers: dict[str, str] | None = None) -> None:
            """Every response: never cached, its length unless a 204, which has no body."""
            self.send_response(code)
            for key, value in {**(headers or {}), "Cache-Control": "no-store"}.items():
                self.send_header(key, value)
            if code != 204:
                self.send_header("Content-Length", str(len(data)))
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
        if (old := viewer_ping()) and old[0] == port:  # two hooks started a viewer at once: the other one runs
            return
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # another program has the port
    server.last_seen = time.monotonic()
    server.viewed = {}  # slug -> when a page last asked for it
    server.restart = False
    atomic_write(VIEWER_FILE, json.dumps({"port": server.server_address[1], "pid": os.getpid()}))

    def needed() -> bool:
        """A page asked lately, or an agent session is on a tracker: its user can open a page at any time."""
        return time.monotonic() - server.last_seen < VIEWER_IDLE_S or bool(live_by_tracker())

    def watchdog():
        """Stop when not needed; restart onto the package's code when it changes and imports."""
        checked = time.monotonic()
        while True:
            time.sleep(2)
            if code_ready():
                server.restart = True
                break
            if time.monotonic() - checked >= SESSION_CHECK_S:
                if not needed():
                    break
                checked = time.monotonic()
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
