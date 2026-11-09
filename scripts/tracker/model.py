"""The tracker's data: records, the Tracker, dependencies, the log and body edits. The contract's constants are
schema's, the text format itself markdown's, the writing of files store's."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from . import store
from .system import die
from .markdown import (BULLET, bullets, frontmatter_problems, link_ident, parse_links, parse_meta, render_frontmatter,
    section, section_span, split_frontmatter, strip_comments, Link)
from .store import HOME, atomic_write, read_json, write_json
from .schema import (BLOCKER, DEFAULT_LABELS, EVIDENCE_DIR, ISSUE, KEYS, KINDS, OPEN_PR, PR_MATCH_TTL_S, PR_STAGE,
    PR_STATES, SCALES, STATE_KEEP_DAYS, STATUSES, csv, id_kind, kind_names, parse_day, show_value, stage_of, today,
    utc_now, utc_seconds, Stage)


def level(t: Record, key: str) -> int | None:
    """A ticket's level on SCALES[key]; None when it has none, or a value `check` refuses."""
    return SCALES[key].parse(t.get(key))


def level_name(t: Record, key: str) -> str:
    """A ticket's level by its name (P1, XS); a value `check` refuses as it is; "" for none."""
    return show_value(key, t.get(key))


# A ticket's spans: name -> (key it starts at, key it ends at, what it measures).
SPANS = {"wait": ("issue_created_at", "started_at", "issue created → started"),
         "cycle": ("started_at", "merged_at", "started → PR merged")}


def span(t: Record, name: str) -> int | None:
    """A ticket's span (SPANS) in seconds; None when dropped, or without both times exact and in order."""
    if t.dropped:
        return None
    start, end = (utc_seconds(t.get(k)) for k in SPANS[name][:2])
    return int(end - start) if start is not None and end is not None and end >= start else None


def state_key(repo: str, branch: str) -> str:
    """A branch's key in .state.json: `owner/name:branch` (a git ref name holds no `:`). A bare branch, from a repo
    with no GitHub remote, stands for that branch in any repo."""
    return f"{repo.lower()}:{branch}" if repo else branch


def same_repo(a: str, b: str) -> bool:
    """Two repos match when they are the same, or when one is not known."""
    return not a or not b or a.lower() == b.lower()


def branch_entry(entries: dict, repo: str, branch: str):
    """A branch's entry in one part of .state.json: under its own key, else under a key that matches its repo."""
    if not branch:
        return None
    exact = entries.get(state_key(repo, branch))
    return exact if exact is not None else next(
        (v for k, v in entries.items() if k.rpartition(":")[2] == branch and same_repo(k.rpartition(":")[0], repo)),
        None)


def put_entry(entries: dict, repo: str, branch: str, value) -> None:
    """Set a branch's entry (None removes it); a bare key from before the repo had a remote goes."""
    entries.pop(branch, None)
    if value is None:
        entries.pop(state_key(repo, branch), None)
    else:
        entries[state_key(repo, branch)] = value


def plain_link(s: str, root: Path | None = None) -> str:
    """Markdown links as `title <url>`, for the AI's plain-text context; an evidence/ link becomes its file's path."""
    def one(m: re.Match) -> str:
        url = m[2]
        if root and url.startswith(EVIDENCE_DIR + "/"):
            url = str(root / url)
        return f"{m[1]} <{url}>"
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", one, s)


def link_url(link: Link) -> str:
    """The URL a link line points to: its markdown link's, else a bare URL, else its text."""
    m = re.search(r"\]\(([^)\s]+)\)", link.text) or re.search(r"https?://\S+", link.text)
    return (m[1] if m.lastindex else m[0]) if m else link.text


def link_title(link: Link) -> str:
    """A link line's title and note, without its URL: `[title](url) — why` gives `title — why`."""
    return re.sub(r"\[([^\]]+)\]\([^)\s]+\)", r"\1", link.text)


def link_lines(items: list[Link], root: Path | None = None, titles: bool = False, width: int = 0,
               urls: bool = True) -> list[str]:
    """Link lines for the AI's context. `titles` keeps only each line's label and title: the short form. `urls` False
    keeps the label, title and why, without the URL. `width` (a brief) cuts each line to that many characters and
    leaves out the nested detail lines."""
    if titles:
        return [f"  {x.label}: {link_title(x).split(' — ')[0]}" for x in items]
    if not urls:
        return [f"  {cut(f'{x.label}: {link_title(x)}', width)}" for x in items]
    out = []
    for link in items:
        out.append(f"  {cut(f'{link.label}: {plain_link(link.text, root)}', width)}")
        out += [] if width else [f"    - {plain_link(x, root)}" for x in link.sub]
    return out


def cut(text: str, width: int) -> str:
    """`short(text, width)`, or the text as it is when width is 0."""
    return short(text, width) if width else text


# ---------------------------------------------------------------- model

@dataclass
class Record:
    path: Path
    kind: str  # a KINDS key, or "tracker" for the README
    meta: dict
    body: str
    problems: list[str] = field(default_factory=list)  # frontmatter lines that did not parse; `check` reports them

    @property
    def id(self) -> str:
        return str(self.meta.get("id") or self.path.stem)

    def get(self, key: str, default=""):
        return self.meta.get(key, default)

    def list(self, key: str) -> list[str]:
        v = self.meta.get(key, [])
        return v if isinstance(v, list) else [v] if v else []

    @property
    def carry_forward(self) -> list[str]:
        return bullets(section(self.body, "Carry forward"))

    @property
    def prs(self) -> dict[str, str]:
        """Its PRs in number order, each number's state. All are equal: what they say together is `pr_state`."""
        items = [str(x).partition(":") for x in self.list("prs")]
        return {n: state for n, _, state in sorted(items, key=lambda x: (len(x[0]), x[0]))}

    @property
    def pr_state(self) -> str:
        """The state of its PRs together: open while one is, then draft, merged, closed; merged only once none is
        open."""
        return next((x for x in PR_STATES if x in self.prs.values()), "")

    @property
    def pr_refs(self) -> str:
        """`#11, #12`, for a line that names its PRs."""
        return ", ".join(f"#{n}" for n in self.prs)

    @property
    def links(self) -> list[Link]:
        return parse_links(section(self.body, "Links"))[0]

    @property
    def aliases(self) -> list[str]:
        """The ids of the ticket's Issue links: another name for the ticket."""
        return [link_ident(x) for x in self.links if x.label == ISSUE] if self.kind == "ticket" else []

    @property
    def stage(self) -> str:
        """Its status; a ticket's, once in progress, overlaid by its PRs' state (`pr_state`, PR_STAGE). A todo ticket on
        a branch that holds a PR has not started, so the PR says nothing about it."""
        status = str(self.get("status", STATUSES[self.kind][0]))
        return PR_STAGE.get(self.pr_state, status) if stage_of(self.kind, status).in_flight else status

    @property
    def progress(self) -> Stage:
        """What its stage says of its work; NO_STAGE for an unknown stage."""
        return stage_of(self.kind, self.stage)

    @property
    def todo(self) -> bool:
        return self.progress.todo

    @property
    def in_flight(self) -> bool:
        return self.progress.in_flight

    @property
    def started(self) -> bool:
        return self.progress.started

    @property
    def dropped(self) -> bool:
        return self.progress.dropped

    @property
    def closed(self) -> bool:
        """A ticket by its stage (a merged PR closes it too); any other record by its status."""
        return self.progress.closed

    def status_update(self, status: str) -> dict:
        """The keys a status change writes, every one of them: the status; `closed_at` now when it closes the record,
        or none when it opens it again. A ticket's first start from todo stamps `started_at`: its wait time ends and
        its cycle time starts. Its close clears `next`: its summary says what it delivered."""
        updates, to = {"status": status}, stage_of(self.kind, status)
        if "closed_at" in KEYS[self.kind]:
            if not to.closed and self.get("closed_at"):
                updates["closed_at"] = None
            elif to.closed and not stage_of(self.kind, self.get("status")).closed:
                updates["closed_at"] = utc_now()
        if self.todo and to.in_flight and not self.get("started_at"):
            updates["started_at"] = utc_now()
        if to.closed and self.get("next"):
            updates["next"] = ""
        return updates

    def change(self, updates: dict | None = None) -> None:
        """Save a change made through the CLI: the updates, and `updated_at` now (the README has none)."""
        self.save({**(updates or {}), **({"updated_at": utc_now()} if "updated_at" in KEYS[self.kind] else {})})

    def save(self, updates: dict) -> None:
        text = self.path.read_text()
        bad = frontmatter_problems(text)
        if bad:
            die(f"{self.path}: {bad[0]} — fix it by hand first")
        lines, body = split_frontmatter(text)
        self.rewrite("---\n" + "\n".join(render_frontmatter(lines, updates)) + "\n---\n" + body)
        self.meta.update(updates)
        for k in [k for k, v in updates.items() if v is None]:
            self.meta.pop(k, None)

    def rewrite(self, text: str) -> None:
        """Write the whole file; the record's body follows it."""
        atomic_write(self.path, text)
        self.body = split_frontmatter(text)[1]


def load_record(path: Path, kind: str) -> Record:
    text = path.read_text()
    lines, body = split_frontmatter(text)
    meta, problems = parse_meta(lines)
    return Record(path, kind, meta, body, frontmatter_problems(text) + problems)


class Tracker:
    def __init__(self, root: Path):
        self.root = root
        self.slug = root.name
        path = root / "README.md"
        # The README as a record, which `set tracker`, the body edits and `migrate` change as they change a ticket; a
        # tracker moved away meanwhile has none.
        self.readme = load_record(path, "tracker") if path.exists() else Record(path, "tracker", {}, "",
                                                                                frontmatter_problems(""))
        self._kinds: dict[str, list[Record]] = {}

    @property
    def meta(self) -> dict:
        return self.readme.meta

    @property
    def readme_body(self) -> str:
        return self.readme.body

    @property
    def readme_problems(self) -> list[str]:
        return self.readme.problems

    def records_of(self, kind: str) -> list[Record]:
        """The records of one kind, by id. Each kind is read on first use: a hook that only matches a branch never
        reads the decisions. A record a command adds joins the list."""
        if kind not in self._kinds:
            self._kinds[kind] = sorted((load_record(p, kind) for p in (self.root / KINDS[kind].folder).glob("*.md")),
                                       key=lambda r: sort_key(r.id))
        return self._kinds[kind]

    @property
    def tickets(self) -> list[Record]:
        return self.records_of("ticket")

    @property
    def decisions(self) -> list[Record]:
        return self.records_of("decision")

    @property
    def actions(self) -> list[Record]:
        """The user's actions (ACTION_BAR)."""
        return self.records_of("action")

    @property
    def records(self) -> list[Record]:
        """Every record, of every kind (KINDS)."""
        return [r for kind in KINDS for r in self.records_of(kind)]

    def open_actions(self) -> list[Record]:
        """The open ones, the soonest due first, then those with no due date."""
        return sorted((a for a in self.actions if not a.closed),
                      key=lambda a: (due_date(a) or dt.date.max, sort_key(a.id)))

    def one_of(self, kind: str, ident: str) -> Record | None:
        """The record of this kind with this id; case and leading zeros do not count."""
        return next((r for r in self.records_of(kind) if norm_id(r.id) == norm_id(ident)), None)

    def next_id(self, kind: str) -> str:
        """A new numbered id of the kind (`D-07`): one past its highest, and the id of no other record."""
        prefix = KINDS[kind].prefix
        n = max((int(m[1]) for r in self.records_of(kind) if (m := re.fullmatch(rf"{prefix}-(\d+)", r.id))), default=0)
        while self.lookup(ident := f"{prefix}-{n + 1:02d}"):
            n += 1
        return ident

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.slug)

    @property
    def context(self) -> list[Link]:
        return parse_links(section(self.readme_body, "Context"))[0]

    @property
    def labels(self) -> list[str]:
        return list(dict.fromkeys(DEFAULT_LABELS + self.readme.list("labels")))

    @property
    def schema(self) -> int:
        try:
            return int(self.meta.get("schema") or 0)
        except ValueError:
            return 0

    @property
    def repos(self) -> list[str]:
        return self.readme.list("repo")

    def repo_of(self, t: Record) -> str:
        """The repo a ticket's branch and PR live in: its own `repo`, or the tracker's only one."""
        return str(t.get("repo") or (self.repos[0] if len(self.repos) == 1 else ""))

    @property
    def archived(self) -> bool:
        return self.root.parent == ARCHIVE

    def headline(self) -> str:
        """`title · status · owner`, the one line that names the work everywhere."""
        return " · ".join(str(x) for x in [self.title, self.meta.get("status"), self.meta.get("owner")] if x)

    def index(self) -> dict:
        """Ids and Issue ids, then (as asked for) each ticket's dependencies and each record's waiting tickets. Built
        once per state of the files: any write through `store`, or a record added to the lists, rebuilds it."""
        key = (store._writes, *(len(self.records_of(kind)) for kind in KINDS))
        if self.__dict__.get("_index_key") != key:
            ids, aliases = {}, {}
            for r in self.records:
                ids.setdefault(norm_id(r.id), r)
            for t in self.tickets:
                for a in t.aliases:
                    aliases.setdefault(norm_id(a), t)
            self._index, self._index_key = {"ids": ids, "aliases": aliases, "deps": {}}, key
        return self._index

    def by_id(self, ident: str) -> Record | None:
        """The record of any kind with this id (a ticket first). Case and leading zeros do not count: `d-4` finds
        D-04."""
        return self.index()["ids"].get(norm_id(ident))

    def lookup(self, ident: str) -> Record | None:
        """`by_id`, else the ticket whose Issue link names the id."""
        return self.by_id(ident) or self.index()["aliases"].get(norm_id(ident))

    def by_pr_or_branch(self, ident: str) -> Record | None:
        """The ticket whose PR is `#123` (`owner/name#123` when the tracker spans repos), or whose branch this is."""
        repo, _, num = ident.rpartition("#")
        if num.isdigit():
            hits = [t for t in self.tickets if num in t.prs
                    and (not repo or self.repo_of(t).lower() == repo.lower())]
            if len(hits) > 1:
                die(f"PR #{num} names {', '.join(t.id for t in hits)}: pass <owner/name>#{num}")
            return hits[0] if hits else None
        return next((t for t in self.tickets if t.get("branch") == ident), None)

    def canonical(self, ident: str) -> str:
        """The id of the record a depends_on item names, else the item as written (an external blocker)."""
        rec = self.lookup(ident)
        return rec.id if rec else ident

    def find(self, ident: str) -> Record:
        """A record by id or Issue id; for a command's argument, also by PR (`#123`) or branch."""
        return (self.lookup(ident) or self.by_pr_or_branch(ident)
                or die(f"no {kind_names()} '{ident}' in {self.slug}"))

    def tickets_on(self, branch: str) -> list[Record]:
        return [t for t in self.tickets if branch and t.get("branch") == branch]

    def open_decisions(self) -> list[Record]:
        return [d for d in self.decisions if not d.closed]

    def decisions_for(self, t: Record) -> list[Record]:
        """The decisions that touch a ticket: those whose refs name it, and those it waits on."""
        waits = {d.ident for d in self.deps(t) if d.kind == "decision"}
        return [d for d in self.decisions if t.id in d.list("refs") or d.id in waits]

    def touched_by(self, d: Record) -> list[str]:
        """The tickets a decision touches: its refs, and the tickets that wait on it."""
        return sorted(set(d.list("refs")) | {t.id for t in self.waiting_on(d.id)}, key=sort_key)

    # -- dependencies: see the section below
    def deps(self, t: Record) -> list[Dep]:
        cache = self.index()["deps"]
        if id(t) not in cache:  # keyed by the record itself; it is kept alive with its deps
            out = []
            for ident in t.list("depends_on"):
                rec = self.lookup(ident)
                out.append(Dep(rec.id, rec) if rec else Dep(ident, link=blocker_link(t, ident)))
            have = {d.ident for d in out}
            cache[id(t)] = (t, out + [Dep(o.id, o) for o in self.stacked_on(t) if o.id not in have])
        return cache[id(t)][1]

    def stacked_on(self, t: Record) -> list[Record]:
        """The tickets past todo on a branch that one of a ticket's open PRs is based on: that PR cannot merge before
        theirs. Computed from each PR's `base`, so it ends when the PR is retargeted."""
        bases = {r["base"] for _, r in self.open_prs(t) if r and r.get("base")}
        return [o for o in self.tickets if o is not t and o.get("branch") in bases and not o.todo
                and same_repo(self.repo_of(o), self.repo_of(t))]

    def blockers(self, t: Record) -> list[Dep]:
        return [d for d in self.deps(t) if not d.done]

    def stackable(self, t: Record) -> bool:
        """Blocked only by tickets already in flight: its work can stack on their branches. Any other open blocker (a
        decision, an action, an external one) still blocks it."""
        blockers = self.blockers(t)
        return bool(blockers) and all(d.rec and d.rec.in_flight for d in blockers)

    def gate(self, r: Record) -> Gate:
        """What stands before a record: a ticket's open blockers, or that it is ready; the tickets an open decision or
        action blocks. The text views and the UI show it the same."""
        if r.kind != "ticket":
            later = tuple(t.id for t in self.waiting_on(r.id))
            return Gate("blocks", later) if later and not r.closed else Gate()
        blockers = () if r.closed else tuple(d.ident for d in self.blockers(r))
        if blockers:
            return Gate("stack" if self.stackable(r) else "blocked", blockers)
        return Gate("ready") if r.todo else Gate()

    def holds(self, t: Record, o: Record, seen: frozenset[str] = frozenset()) -> bool:
        """t's branch holds o's work: they share a branch, or t's open PR is based on o's branch, directly or through
        other tickets' branches."""
        if o is t or (t.get("branch") and t.get("branch") == o.get("branch")
                      and same_repo(self.repo_of(t), self.repo_of(o))):
            return True
        return any(x.id not in seen and self.holds(x, o, seen | {t.id}) for x in self.stacked_on(t))

    def start_point(self, t: Record) -> Start | None:
        """Where a todo ticket's work can start (START_RULE); None when it is not todo, or when more than tickets under
        way blocks it. Tickets under way in another repo give no branch to start from."""
        if not t.todo or (self.blockers(t) and not self.stackable(t)):
            return None
        under = [d.rec for d in self.blockers(t) if same_repo(self.repo_of(d.rec), self.repo_of(t))]
        base = next((b for b in under if all(self.holds(b, o) for o in under)), None)
        return Start(base, [] if base else under)

    def startable(self) -> list[tuple[Record, Start]]:
        """The todo tickets that can start now, each with where it starts: the most urgent first, then by id."""
        def urgency(ts: tuple[Record, Start]) -> tuple[bool, int]:
            p = level(ts[0], "priority")
            return p is None, p or 0
        return sorted(((t, s) for t in self.tickets if (s := self.start_point(t))), key=urgency)

    def waiting_on(self, ident: str) -> list[Record]:
        """The tickets whose depends_on names this ticket, decision or action."""
        index = self.index()
        if "waiting" not in index:
            waiting: dict[str, list[Record]] = {}
            for t in self.tickets:
                for i in dict.fromkeys(d.ident for d in self.deps(t)):
                    waiting.setdefault(i, []).append(t)
            index["waiting"] = waiting
        return list(index["waiting"].get(ident, []))

    def ready(self) -> list[Record]:
        """The todo tickets that nothing blocks."""
        return [t for t in self.tickets if t.todo and not self.blockers(t)]

    @cached_property
    def prs(self) -> dict[str, dict]:
        """The PR_FACTS `sync` keeps per open PR (STATE_RULES `prs`)."""
        prs = self.raw_state().get("prs", {})
        return prs if isinstance(prs, dict) else {}

    def open_prs(self, t: Record) -> list[tuple[str, dict | None]]:
        """A ticket's open PRs, by number, each with its PR_FACTS; None before `sync` read them."""
        return [(n, self.prs.get(pr_key(self.repo_of(t), n))) for n, state in t.prs.items() if state in OPEN_PR]

    # -- machine state (.state.json): see STATE_RULES. state() drops what no longer applies, so a write of what it
    # read keeps the file current; raw_state() is the file as it is.
    def raw_state(self) -> dict:
        return read_json(self.root / ".state.json")

    def state(self) -> dict:
        state, now = self.raw_state(), time.time()
        use = {}
        for key, ids in state.get("use", {}).items():
            ids = [i for i in ids if (r := self.lookup(i)) and r.kind == "ticket" and not r.closed]
            if ids:
                use[key] = ids

        def current(part: str, key: str, entry) -> bool:
            repo, _, branch = key.rpartition(":")
            on = [t for t in self.tickets if t.get("branch") == branch and same_repo(self.repo_of(t), repo)]
            if any(not t.closed for t in on) or any(k.endswith("@" + branch) for k in use):
                return True
            return now - entry.get("at", 0) < STATE_KEEP_DAYS * 86400 and (part == "synced" or not on)

        out = dict(state, use=use)
        for part in ("synced", "handoff"):
            out[part] = {k: v for k, v in state.get(part, {}).items() if current(part, k, v)}
        out["pr_match"] = {k: v for k, v in state.get("pr_match", {}).items() if now - v.get("at", 0) < PR_MATCH_TTL_S}
        return {k: v for k, v in out.items() if v != {}}

    def issue_reads(self) -> tuple[dict[str, float], float]:
        """When `tracker issue` last read each ticket's issue fields, and when a UI's Refresh asked for them
        again (STATE_RULES["issues"])."""
        issues = self.raw_state().get("issues", {})
        return issues.get("read", {}), issues.get("requested", 0)

    def issues_to_read(self) -> list[Record]:
        """The tickets whose issue fields the model should read from their issue tracker (STATE_RULES["issues"])."""
        read, asked = self.issue_reads()
        return [t for t in self.tickets if t.aliases and (not t.closed and (
            not read.get(t.id) or read[t.id] < asked)
            or not read.get(t.id) and t.get("started_at"))]  # closed: once, for its wait time, if it has a start

    def issue_shown(self, t: Record) -> dict:
        """What t's issue should show (STATE_RULES["issues"]): its stage, its summary once closed, and the answer of
        each settled decision that touches it."""
        return {"stage": t.stage, "summary": (t.get("summary") or None) if t.closed else None,
                "decisions": {d.id: answer(d) for d in self.decisions_for(t) if d.closed}}

    def issue_updates_due(self) -> list[tuple[Record, list[str]]]:
        """Per linked ticket, what its issue should now show and does not yet, as items for the model to write with
        the issue tracker's tool (STATE_RULES["issues"]): its stage, with its summary once closed; and each settled
        decision that touches it, by title and answer. Nothing while README `issue_updates` is not on, or before
        `set` recorded the history."""
        issues = self.raw_state().get("issues", {})
        if self.readme.get("issue_updates") != "on" or "history" not in issues:
            return []
        written, history = issues.get("written", {}), issues["history"]
        out = []
        for t in self.tickets:
            if not t.aliases:
                continue
            last, now = written.get(t.id, {}), self.issue_shown(t)
            items = []
            if (now["stage"], now["summary"]) != (last.get("stage"), last.get("summary")) and (last or not t.todo):
                items.append(f"status {now['stage']}" + (f"; summary: {now['summary']}" if now["summary"] else ""))
            told = {**history, **last.get("decisions", {})}
            items += [f"decision settled: {d.get('title')}: {now['decisions'][d.id]}" for d in self.decisions_for(t)
                      if d.closed and now["decisions"][d.id] != told.get(d.id)]
            if items:
                out.append((t, items))
        return out

    def list_issue_updates(self, due: list[tuple[Record, list[str]]]) -> None:
        """`tracker issue` listed these updates: what `--written` then records for each ticket."""
        state = self.raw_state()
        state.setdefault("issues", {})["listed"] = {t.id: self.issue_shown(t) for t, _ in due}
        self.save_state(state)

    def mark_issue_written(self, t: Record) -> bool:
        """Record that t's issue now shows what `tracker issue` last listed for it; False when it listed nothing for
        t. A change after the listing stays due."""
        state = self.raw_state()
        issues = state.setdefault("issues", {})
        shown = issues.get("listed", {}).pop(t.id, None)
        if shown is None:
            return False
        issues.setdefault("written", {})[t.id] = {**shown, "at": time.time()}
        self.save_state(state)
        return True

    def start_issue_updates(self) -> None:
        """README `issue_updates` is on: the decisions settled before it first was are history, never due. Kept once
        recorded, so turning updates off and on again catches up on what was settled since."""
        state = self.raw_state()
        issues = state.setdefault("issues", {})
        if "history" not in issues:
            issues["history"] = {d.id: answer(d) for d in self.decisions if d.closed}
            self.save_state(state)

    def save_state(self, state: dict) -> None:
        write_json(self.root / ".state.json", state, indent=2)

    def data_files(self) -> list[Path]:
        """Every file that holds the tracker's facts: a change to one is a change a UI and the watcher show."""
        return [self.root / "README.md", self.root / "log.md", self.root / ".state.json",
                *(p for kind in KINDS.values() for p in sorted((self.root / kind.folder).glob("*.md")))]


def files_hash(files: list[Path]) -> str:
    """Changes when any of the files does: their names, times and sizes. A missing file counts as none."""
    stats = []
    for p in files:
        try:
            s = p.stat()
        except OSError:
            continue
        stats.append(f"{p.name}:{s.st_mtime_ns}:{s.st_size}")
    return hashlib.sha1("|".join(stats).encode()).hexdigest()[:12]


def sort_key(ident: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", ident)]


def norm_id(ident: str) -> tuple:
    """An id as lookups compare it: case and leading zeros do not count."""
    return tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", ident.strip()))


def id_list(values: list[str] | None) -> list[str]:
    """Ids given as one comma list, as separate words, or both: `--ref SS-2,D-03` = `--ref SS-2 D-03`."""
    return csv(",".join(values or []))


def all_trackers(home: Path = HOME) -> list[Tracker]:
    if not home.exists():
        return []
    return [Tracker(p) for p in sorted(home.iterdir()) if p.is_dir() and (p / "README.md").exists()]


def tracker_at(slug: str, home: Path = HOME) -> Tracker | None:
    """The tracker named `slug`, if it exists. A slug is one folder name in HOME, never a path."""
    slug = str(slug or "")
    return Tracker(home / slug) if SAFE_NAME.fullmatch(slug) and (home / slug / "README.md").exists() else None


# An archived tracker is a folder in ARCHIVE. Every list, lookup, hook and sync reads HOME alone, so it costs nothing
# there; a UI still shows it, and `tracker unarchive` brings it back. A slug names one tracker, archived or not.
ARCHIVE = HOME / ".archive"


def archived_trackers() -> list[Tracker]:
    return all_trackers(ARCHIVE)


def archived_at(slug: str) -> Tracker | None:
    return tracker_at(slug, ARCHIVE)


def any_tracker(slug: str) -> Tracker | None:
    """The tracker named `slug`, archived or not: a UI shows both."""
    return tracker_at(slug) or archived_at(slug)


RESOLUTION_LINE = re.compile(r"^(?:- )?\d{4}-\d{2}-\d{2}\b")


def answer(d: Record) -> str:
    """A closed decision's answer alone: its resolution without the leading day and who settled it."""
    text = resolution(d)
    return text.split(": ", 1)[1] if RESOLUTION_LINE.match(text) and ": " in text else text


def resolution(d: Record) -> str:
    """A closed record's answer (a decision's): the latest dated `YYYY-MM-DD (who): answer` line of its kind's closing
    section (## Resolution), or, when a person wrote it without one, its first line (detail lines follow the answer)."""
    lines = [ln.strip() for ln in strip_comments(section(d.body, KINDS[d.kind].closing)).splitlines() if ln.strip()]
    dated = [ln for ln in lines if RESOLUTION_LINE.match(ln)]
    return (dated or lines or [""])[-1 if dated else 0].removeprefix("- ")


# ---------------------------------------------------------------- dependencies
# A ticket's `depends_on` is the one place an order or a blocker is written, always on the ticket that waits. Each
# item is a ticket (satisfied once it is done or dropped), a decision or a user's action (satisfied once
# closed), or any other id: an external blocker, satisfied only when it is removed, and named by a `- Blocker:` line
# in the ticket's ## Links that says where it is and why it blocks. An open PR based on another ticket's branch also
# waits on that ticket: that comes from the PR's `base`, not depends_on. Blockers, "unblocks", steps, the critical
# path and the ready list are all computed.



@dataclass
class Dep:
    ident: str  # the record's id (a depends_on item, an id or Issue id, resolves to it), or the external id as written
    rec: Record | None = None  # None for an external blocker
    link: Link | None = None  # an external blocker's line in ## Links

    @property
    def kind(self) -> str:
        return self.rec.kind if self.rec else "external"

    @property
    def done(self) -> bool:
        return bool(self.rec and self.rec.closed)

    def describe(self) -> str:
        """`T-5 in-review`, `D-10 open: <title>`, `A-2 open: <title>`, `X external: <why>`, for the AI's plain-text
        context."""
        if not self.rec:
            return f"{self.ident} external" + (f": {plain_link(self.link.text)}" if self.link else "")
        if self.done:
            return f"{self.ident} {self.rec.stage} ✓"
        return f"{self.ident} {self.rec.stage}" + (f": {self.rec.get('title')}" if self.kind != "ticket" else "")


@dataclass(frozen=True)
class Gate:
    """What stands before a record (Tracker.gate). `state`: `ready`, a todo ticket that nothing blocks; `stack`, a
    ticket that waits only on work under way, so it can stack on it; `blocked`, a ticket with any other open blocker;
    `blocks`, an open decision or action that tickets wait on; "" for none. `ids`: its blockers, or the tickets it
    blocks."""
    state: str = ""
    ids: tuple[str, ...] = ()

    def text(self) -> str:
        """`waits on T-5, D-10`, `stacks on T-4`, `blocks T-2`, `ready`; "" for none."""
        verb = {"stack": "stacks on", "blocked": "waits on", "blocks": "blocks"}.get(self.state)
        return f"{verb} {', '.join(self.ids)}" if verb else self.state


@dataclass
class Start:
    """Where a todo ticket's work can start (Tracker.start_point): from the default branch (no `base`, nothing
    `apart`); stacked on `base`'s branch, which holds the work of every ticket under way that it waits on; or, when
    those tickets are on separate branches (`apart`), stacked on one of them once someone chooses."""
    base: Record | None = None
    apart: list[Record] = field(default_factory=list)

    @property
    def stacked(self) -> bool:
        return bool(self.base or self.apart)


def names(link: Link, ident: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(ident)}(?![\w-])", link.text, re.I))


def unknown_dep(tr: Tracker, ident: str) -> str:
    """Why an item of a ticket's depends_on names no record of the tracker."""
    kind = id_kind(ident)
    return f"no {kind} {ident}" if kind else f"{ident} is not a {kind_names()} in {tr.slug}"


def blocker_link(t: Record, ident: str) -> Link | None:
    """The `- Blocker:` line in a ticket's ## Links that names an external blocker."""
    return next((x for x in t.links if x.label == BLOCKER and names(x, ident)), None)


@dataclass
class Sequence:
    step: dict[str, int]  # per ticket: 1 + the longest chain of tickets it waits on
    critical: list[str]  # the longest chain of unfinished tickets, first to last
    cycles: list[list[str]]
    waits: dict[str, list[str]]  # per ticket: the tickets it waits on, its depends_on and those its open PR stacks on


def sequence(tr: Tracker) -> Sequence:
    edges = {t.id: list(dict.fromkeys(d.ident for d in tr.deps(t) if d.kind == "ticket" and d.ident != t.id))
             for t in tr.tickets}
    step, cycles, path = {}, [], []

    def visit(i: str) -> int:
        if i in step:
            return step[i]
        if i in path:
            cycle = path[path.index(i):] + [i]
            if not any(set(c) == set(cycle) for c in cycles):
                cycles.append(cycle)
            return 0
        path.append(i)
        s = 1 + max((visit(j) for j in edges[i]), default=0)
        path.pop()
        step[i] = s
        return s

    for i in edges:
        visit(i)
    return Sequence(step, [] if cycles else longest_open_chain(tr, edges), cycles, edges)


def longest_open_chain(tr: Tracker, edges: dict[str, list[str]]) -> list[str]:
    unfinished = {t.id for t in tr.tickets if not t.closed}
    depth: dict[str, int] = {}

    def d(i: str) -> int:
        if i not in depth:
            depth[i] = 1 + max((d(j) for j in edges[i] if j in unfinished), default=0)
        return depth[i]

    ends = sorted(unfinished, key=lambda i: (-d(i), sort_key(i)))
    if not ends or d(ends[0]) < 2:
        return []
    chain = [ends[0]]
    while prev := sorted((j for j in edges[chain[-1]] if j in unfinished and d(j) == d(chain[-1]) - 1), key=sort_key):
        chain.append(prev[0])
    return chain[::-1]


def pr_key(repo: str, pr) -> str:
    """A PR's key in .state.json `prs`: `owner/name#n`."""
    return f"{repo.lower()}#{pr}"


def unblocked(tr: Tracker, blocked: set[str]) -> list[str]:
    """A line naming the unfinished tickets in `blocked` that nothing blocks now."""
    free = [t.id for t in tr.tickets if t.id in blocked and not tr.blockers(t) and not t.closed]
    return [f"nothing blocks {', '.join(free)} now (to start one: `tracker set <id> status=in-progress`)"] \
        if free else []


# ---------------------------------------------------------------- the log
# log.md holds one line per change, `- <day> [<ids>] <text>`: append_log writes it and LOG_LINE reads it back. Most
# texts are for people. Those that code reads back have a LogForm in LOG, which both writes them and reads them, so a
# change of words cannot part the two: `context` and the brief leave out the lines of a decision they show, and
# the watcher counts the commits and marks a new action as one for the user.

LOG_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2})(?: \[([^\]]*)\])? ")  # group 1 its day, group 2 its ids


def append_log(tr: Tracker, msg: str, refs: list[str]) -> str:
    line = f"- {today()}" + (f" [{' '.join(refs)}]" if refs else "") + f" {' '.join(msg.split())}"
    with open(tr.root / "log.md", "a") as f:
        f.write(line + "\n")
    return line


@dataclass(frozen=True)
class LogForm:
    """A log text that code reads back: `text` writes it from `template`, `read` matches its start, and group 1 of
    the match is what the reader needs. A writer may add more after it."""
    template: str
    pattern: re.Pattern

    def text(self, **fields) -> str:
        return self.template.format(**fields)

    def read(self, text: str) -> re.Match | None:
        return self.pattern.match(text)


DECISION_ID = rf"{KINDS['decision'].prefix}-\d+"
LOG = {
    "opened": LogForm("Opened {id} {title}", re.compile(rf"Opened ({DECISION_ID})\b")),
    "decided": LogForm("Decided {id} {title}{who}: {answer}", re.compile(rf"Decided ({DECISION_ID})\b")),
    "action": LogForm("Action for the user: {title}", re.compile(r"Action for the user: (.*)")),
    "commits": LogForm("Commits on {branch}: {commits}", re.compile(r"Commits on \S+: (.*)")),
}
COMMITS_LOGGED_MAX = 8  # the commits a log line names; the rest as a count


def commits_text(branch: str, commits: list[tuple[str, str]]) -> str:
    """`Commits on <branch>: <sha> <subject>; ...; and 3 more`."""
    shown = "; ".join(f"{sha} {subject}" for sha, subject in commits[:COMMITS_LOGGED_MAX])
    more = f"; and {len(commits) - COMMITS_LOGGED_MAX} more" if len(commits) > COMMITS_LOGGED_MAX else ""
    return LOG["commits"].text(branch=branch, commits=shown + more)


def commits_logged(text: str) -> int:
    """How many commits a log text that commits_text wrote names; 0 for any other text."""
    m = LOG["commits"].read(text)
    if not m:
        return 0
    items = m[1].split("; ")
    more = re.fullmatch(r"and (\d+) more", items[-1])
    return len(items) - 1 + int(more[1]) if more else len(items)


def logged_decision(text: str) -> str | None:
    """The decision a log text opened or settled; None for any other text."""
    m = LOG["opened"].read(text) or LOG["decided"].read(text)
    return m[1] if m else None


PR_EVENT = {"merged": "merged", "open": "open for review", "draft": "open as a draft", "closed": "closed unmerged"}


def apply_prs(tr: Tracker, found: dict[str, tuple[str, list[dict]]], facts: dict[str, dict] | None) -> list[str]:
    """Write what a forge's `sync` read, in the tracker's words: per ticket id its repo and PRs (each with `number`;
    `state`: one of PR_EVENT; `merged_at`, UTC), each PR's new state a log line; and the open PRs' PR_FACTS by pr_key
    (None: the forge did not answer, so the last ones stay). Returns the change lines."""
    changes, events = [], {}
    blocked = {t.id for t in tr.tickets if tr.blockers(t)}
    for t in tr.tickets:
        if t.id not in found:
            continue
        repo, prs = found[t.id]
        old = t.prs
        now = sorted(prs, key=lambda x: int(x["number"]))
        written = [f"{x['number']}:{x['state']}" for x in now]
        updates = {"prs": written} if list(map(str, t.list("prs"))) != written else {}
        if next((s for s in PR_STATES if any(x["state"] == s for x in now)), "") == "merged":  # as Record.pr_state
            merged = max(x["merged_at"] for x in now if x["state"] == "merged")
            if str(t.get("merged_at")) != merged:
                updates["merged_at"] = merged
        if updates:
            t.change(updates)  # one write
        if "prs" in updates:
            changes.append(f"{t.id}: PRs " + ", ".join(written))
        for x in now:
            if old.get(str(x["number"])) != x["state"]:
                name = f"{repo}#{x['number']}" if len(tr.repos) > 1 else f"#{x['number']}"
                events.setdefault((name, x["state"]), []).append(t.id)
    for (name, pr_state), ids in events.items():
        append_log(tr, f"PR {name} {PR_EVENT[pr_state]}", ids)
    changes += unblocked(tr, blocked)
    state = tr.state()
    state["last_sync"] = time.time()
    if facts is not None:
        state["prs"] = facts
    tr.save_state(state)
    return changes


def due_date(r: Record) -> dt.date | None:
    """A record's due day; None when it has none, or one `check` refuses."""
    return parse_day(r.get("due"))


def close_action(tr: Tracker, a: Record, status: str, notes: list[str] | None = None) -> str:
    """Close a user's action as done or dropped, with its outcome notes when it has some; log it. Returns what it
    did."""
    if a.closed:
        die(f"{a.id} is {a.get('status')} already")
    a.change(a.status_update(status))
    append_notes(a, notes or [])
    append_log(tr, f"{a.id} {status}: {a.get('title')}" + (f" → {short('; '.join(notes))}" if notes else ""),
               [a.id, *a.list("refs")])
    return f"{a.id} {status}"


def append_notes(a: Record, notes: list[str]) -> None:
    """A bullet per note at the end of an action's body, one list. Undated: the log line each write adds dates it."""
    if not notes:
        return
    text = a.path.read_text().rstrip("\n")
    gap = "\n" if BULLET.match(strip_comments(text).rstrip().splitlines()[-1]) else "\n\n"
    a.rewrite(text + gap + "".join(f"- {' '.join(n.split())}\n" for n in notes))


def set_branch(tr: Tracker, t: Record, branch: str, note: str = "") -> None:
    """Record the branch a ticket is built on, where sync finds its PR and every worktree finds the ticket; log it,
    with `note` (how the branch was found)."""
    t.change({"branch": branch})
    append_log(tr, f"{t.id} is built on branch {branch}" + (f" ({note})" if note else ""), [t.id])


def short(text: str, n: int = 90) -> str:
    """One line of at most n characters."""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a ticket id, tracker slug or session id: also a file name


# ---------------------------------------------------------------- body edits
# Every command that changes a record's body goes through one of these, or `append_notes` for an action's notes; each
# changes one section in place.

def relabel(rec: Record, link: Link, label: str) -> bool:
    """Change one ## Links line's label in place."""
    text = rec.path.read_text()
    pat = re.compile(rf"^- (?:\*\*)?{re.escape(link.label)}(?:\*\*)?:(?:\*\*)?(\s+){re.escape(link.text)}$", re.M)
    new, n = pat.subn(lambda m: f"- {label}:{m[1]}{link.text}", text, count=1)
    if n:
        rec.rewrite(new)
    return bool(n)


def append_to_section(rec: Record, heading: str, line: str) -> None:
    """Add text at the end of a section: a bullet joins the list before it; anything else is a new paragraph."""
    text = rec.path.read_text()
    m = section_span(text, heading)
    if not m:
        text = text.rstrip("\n") + f"\n\n## {heading}\n\n{line}\n"
    else:
        block = strip_comments(m.group(0)).rstrip()
        last = block.splitlines()[-1]
        gap = "\n" if BULLET.match(line) and (BULLET.match(last) or last.startswith("  ")) else "\n\n"
        rest = text[m.end():].lstrip("\n")
        text = text[:m.start()] + f"{block}{gap}{line}\n" + (f"\n{rest}" if rest else "")
    rec.rewrite(text)


def replace_in_section(rec: Record, heading: str, old: str, new: str) -> None:
    """Replace text that occurs once in a section."""
    text = rec.path.read_text()
    m = section_span(text, heading)
    block = m.group(0) if m else ""
    n = block.count(old)
    if n != 1:
        die(f"{rec.id} ## {heading} holds '{short(old, 50)}' {n} times; pass text that occurs once")
    rec.rewrite(text[:m.start()] + block.replace(old, new) + text[m.end():])


def put_section(rec: Record, heading: str, new: str) -> None:
    """Replace a section's whole text, its template comment too; a section the file lacks is added at its end."""
    text = rec.path.read_text()
    m = section_span(text, heading)
    block = f"## {heading}\n\n{new.strip()}\n"
    if not m:
        rec.rewrite(text.rstrip("\n") + f"\n\n{block}")
        return
    rest = text[m.end():].lstrip("\n")
    rec.rewrite(text[:m.start()] + block + (f"\n{rest}" if rest else ""))


def drop_from_section(rec: Record, heading: str, needle: str) -> int:
    """Remove the one line of a section that holds `needle`; a bullet goes with its nested lines. Returns the number
    of lines removed."""
    text = rec.path.read_text()
    m = section_span(text, heading)
    lines = (m.group(0) if m else "").split("\n")
    hits = [i for i, ln in enumerate(lines) if i and needle in ln]
    if len(hits) != 1:
        die(f"{rec.id} ## {heading} has {len(hits)} lines with '{short(needle, 50)}'; pass text one line holds")
    i = end = hits[0]
    end += 1
    if BULLET.match(lines[i]):
        depth = len(lines[i]) - len(lines[i].lstrip())
        while end < len(lines) and lines[end].strip() and len(lines[end]) - len(lines[end].lstrip()) > depth:
            end += 1
    rec.rewrite(text[:m.start()] + "\n".join(lines[:i] + lines[end:]) + text[m.end():])
    return end - i
