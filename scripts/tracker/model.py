"""The tracker's data: the contract's constants (keys, statuses, sections, labels), records, the Tracker,
dependencies, body edits and the write lock. The text format itself is markdown's."""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from .markdown import (BULLET, bullets, frontmatter_problems, link_ident, parse_links, parse_meta,
    render_frontmatter, section, section_span, split_frontmatter, strip_comments, Link)

HOME = Path(os.environ.get("TRACKER_HOME", Path.home() / ".claude" / "trackers")).expanduser()
PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent.parent  # the plugin: bin/, scripts/, templates/, viewer/
BIN = ROOT / "bin" / "tracker"  # the CLI
# This Python, as scripts/python.sh says to start it: no module in the cwd replaces a stdlib one; UTF-8 mode
PYTHON = [sys.executable, "-I", "-X", "utf8"]
# The CLI on this Python, as bin/tracker starts it: Windows starts no shell script as a process
CLI = [*PYTHON, "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from tracker.cli import main; main()",
       str(PACKAGE.parent)]
WINDOWS = os.name == "nt"

TICKET_STATUSES = ["todo", "in-progress", "done", "dropped"]  # stored: what you set
STAGES = ["todo", "in-progress", "in-review", "merged", "done", "dropped"]  # shown: the status, overlaid by the PR
PR_STAGE = {"draft": "in-progress", "open": "in-review", "merged": "merged"}  # pr_state -> stage of started work
CLOSED_TICKET = {"merged", "done", "dropped"}  # stages
DECISION_STATUSES = ["open", "closed"]
LIST_KEYS = {"depends_on", "refs", "labels"}
LIST_OR_ONE = {"repo"}  # one value, or a list when the work spans repos

# Every frontmatter key per file: what writes it ("set" = `tracker set`, or the command that owns it) and what it holds.
KEYS = {
    "ticket": {
        "id": ("new", "the ticket's id; also its file name"),
        "title": ("set", "short name of the ticket"),
        "status": ("set", "the work status; once in-progress, the stage shown is this overlaid by the PR"),
        "group": ("set", "a free label that groups tickets in the views; not an order"),
        "branch": ("set", "the git branch the ticket is built on; finds the ticket and its PR. A branch holds any "
                          "number of tickets. `set status=in-progress` records the current branch when empty"),
        "repo": ("set", "owner/name of the ticket's repo, when the tracker spans several"),
        "depends_on": ("wait", "what the ticket waits on: ticket, decision or external ids; the only record of "
                               "order and blockers"),
        "next": ("set", "one concrete next action, true as of now; cleared when the ticket closes"),
        "summary": ("set", "one line: what the ticket delivered or why it was dropped; shown once it is closed"),
        "updated": ("auto", "date of the last change through the CLI"),
        "started_at": ("auto", "when `set status=in-progress` first started the ticket, UTC (YYYY-MM-DDTHH:MM:SSZ)"),
        "pr": ("sync", "the PR number"),
        "pr_state": ("sync", "draft|open|merged|closed, from the PR"),
        "base": ("sync", "the PR's base branch; while the PR is open, a base that is other tickets' branch makes "
                         "this ticket wait on them (computed; depends_on does not change)"),
        "merged_at": ("sync", "when the PR merged, UTC (YYYY-MM-DDTHH:MM:SSZ); a date alone before 0.29"),
        "priority": ("issue", "the priority the ticket's issue has in its issue tracker, in that tracker's words "
                              "(High, P1, Urgent)"),
        "issue_created": ("issue", "when the ticket's issue was created in its issue tracker, UTC "
                                   "(YYYY-MM-DDTHH:MM:SSZ)"),
    },
    "decision": {
        "id": ("decide", "D-<n>; also the file name"),
        "title": ("set", "short name of the decision"),
        "status": ("decide", "open until `decide --resolve` closes it; the answer is ## Resolution"),
        "refs": ("decide", "tickets it touches but does not block; a block is the ticket's depends_on"),
        "owner": ("set", "who must decide"),
        "opened": ("auto", "date it was opened"),
        "updated": ("auto", "date of the last change through the CLI"),
    },
    "tracker": {
        "title": ("set", "name of the work"),
        "repo": ("set", "GitHub owner/name, or a list when the work spans repos; enables `sync`"),
        "status": ("set", "the work's status"),
        "owner": ("set", "who owns the work"),
        "created": ("auto", "date the tracker was made"),
        "labels": ("set", "this tracker's own link labels, added to the defaults"),
        "schema": ("auto", "the tracker's format version; `tracker migrate` brings an older one up to date"),
    },
}
SCHEMA = 1  # the tracker format this code writes; README `schema` names a tracker's (none: older than 1)
RETIRED_KEYS = {"ticket": {"slice", "key"}, "decision": {"resolved"}, "tracker": set()}  # `migrate` removes them
OWNER_HINT = {"new": "fixed at `tracker new`", "decide": "use `tracker decide`", "wait": "use `tracker wait`",
              "sync": "`tracker sync` writes it from the PR", "auto": "the tracker writes it",
              "issue": "`tracker issue` writes it from the ticket's issue tracker"}
# Machine state in .state.json: what `tracker rules` says about it. Never in frontmatter. A branch's entries are keyed
# `owner/name:branch` (see state_key), so a tracker can span repos.
STATE_KEEP_DAYS = 14
STATE_RULES = {
    "mark": "the HEAD up to which a branch's commits are logged. The hooks log them against its tickets under way "
            "(after a commit, at a turn's end, at session start) and move it; `step`, `synced` and `pause` log any "
            "they missed. With no ticket under way, commits wait unlogged for the ticket the session starts. It also "
            "counts the commits logged since the branch's tickets' `next` last changed. Uncommitted files written "
            "after it are work the tracker may not show yet",
    "handoff": "`pause \"<text>\"` (or `step --pause`) leaves the state of unfinished work on a branch for the next "
               "session; the brief shows it first; the next `step` or `synced` clears it",
    "use": "`tracker use` on a shared branch such as main: the tickets one worktree works on",
    "pr_match": "a branch's PR, asked for once in a while to find its ticket",
    "reviews": "per open PR (`owner/name#n`), what `sync` last read of its reviews, checks and merge state; each sync "
               "replaces them. A ticket's move is computed from them",
    "issues": "`read`: per ticket, when `tracker issue` last recorded its issue's fields; `requested`: when the "
              "viewer's Refresh asked for them again. A ticket with an Issue link is due when never read, or when "
              "open and read more than a day ago or before the request; the brief and the prompt hook list the due "
              "ones for the model, which reads them with the issue tracker's tool",
    "cleanup": f"while a branch has an unfinished ticket its entries stay. Otherwise a handoff goes once the "
               f"branch's tickets are closed, and a mark, or a handoff on a branch no ticket is on, {STATE_KEEP_DAYS} "
               f"days after it was set. `use` keeps only unfinished tickets",
}


ISSUE_STALE_S = 86400  # an open ticket's issue fields are read again after this

# Issue trackers' priority words, most urgent first: Shortcut and Jira (Highest … Lowest), Linear (Urgent … Low),
# and P0-P9. A word not here ranks after them; "none" and "no priority" are no priority.
PRIORITY_RANKS = {"urgent": 0, "highest": 0, "critical": 0, "blocker": 0, "high": 1, "medium": 2, "normal": 2,
                  "low": 3, "lowest": 4, "trivial": 4}
PRIORITY_UNKNOWN = 9

# A ticket's spans: name -> (key it starts at, key it ends at, what it measures). Each needs both times exact (UTC to
# the second) and in order. A dropped ticket has none.
SPANS = {"wait": ("issue_created", "started_at", "issue created → started"),
         "cycle": ("started_at", "merged_at", "started → PR merged")}


def utc_seconds(text: str) -> int | None:
    """A `YYYY-MM-DDTHH:MM:SSZ` time as epoch seconds; None for anything else, a date alone included."""
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def span(t: Record, name: str) -> int | None:
    """A ticket's span (SPANS) in seconds; None when dropped, or without both times exact and in order."""
    if t.stage == "dropped":
        return None
    start, end = (utc_seconds(t.get(k)) for k in SPANS[name][:2])
    return int(end - start) if start is not None and end is not None and end >= start else None


def priority_rank(text: str) -> int | None:
    """Where a priority sorts, most urgent first (0); None for no priority."""
    word = text.strip().lower()
    if word in ("", "none", "no priority"):
        return None
    if re.fullmatch(r"p\d", word):
        return int(word[1])
    return PRIORITY_RANKS.get(word, PRIORITY_UNKNOWN)


def state_key(repo: str, branch: str) -> str:
    """A branch's key in .state.json: `owner/name:branch` (a git ref name holds no `:`). A bare branch, from a repo
    with no GitHub remote or an older version, stands for that branch in any repo."""
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
    """Set a branch's entry (None removes it); an older bare key for the branch goes."""
    entries.pop(branch, None)
    if value is None:
        entries.pop(state_key(repo, branch), None)
    else:
        entries[state_key(repo, branch)] = value


README_TOKEN_BUDGET = 2000
MERGED_CARRY_FORWARD_MAX = 5
STALE_DECISION_DAYS = 14
STALE_TICKET_DAYS = 7
SYNC_MIN_INTERVAL_S = 600
PR_MATCH_TTL_S = 600  # how long a branch's PR, once asked, is not asked again


def dated(text: str) -> str:
    """`<today>: text`; a text that starts with a date keeps its own."""
    return text if re.match(r"\d{4}-\d{2}-\d{2}\b", text) else f"{today()}: {text}"


def today() -> str:
    return dt.date.today().isoformat()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def die(msg: str, code: int = 2) -> None:
    print(f"tracker: {msg}", file=sys.stderr)
    sys.exit(code)


def spawn(*args: str) -> None:
    """Start `tracker <args>` in the background: it outlives this process and writes to no terminal."""
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
              else {"start_new_session": True})
    subprocess.Popen([*CLI, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **detach)


# The longest text a command takes, in characters: a view shows it whole or cuts it near here (a brief cuts a line at
# 200), so a longer text loses its end in every session. Enforced when written; `check` leaves older text alone.
TEXT_MAX = {  # kind: (characters, what it is, where the rest goes)
    "next": (200, "next", "one concrete action; the detail goes in the Plan or the PR"),
    "summary": (200, "summary", "one line; the detail goes in the PR"),
    "log": (200, "log line", "what changed and why, in short; the detail goes in the PR, the commits or the ticket"),
    "carry": (400, "Carry forward bullet", "one fact per bullet: split it into more"),
    "priority": (40, "priority", "use the issue tracker's own word for it"),
}


def fit(kind: str, text: str | None) -> None:
    """Refuse a text longer than TEXT_MAX allows."""
    most, what, rest = TEXT_MAX[kind]
    n = len(" ".join(str(text or "").split()))
    if n > most:
        die(f"the {what} is {n} characters, over {most}: {rest}")


# ---------------------------------------------------------------- template contract
# Every tracker has the same shape, and `check` enforces it, so the viewer and the AI find the same things in the
# same place. The templates/ folder shows each file's shape.

README_SECTIONS = ["Context", "Goal", "Scope"]  # required, first, in this order; any sections may follow
README_KEYS = ["title", "repo", "status", "owner", "created"]  # required frontmatter; repo may be empty
SCOPE_PARTS = ["In", "Out"]  # ### subsections of ## Scope
TRACKER_STATUSES = ["planning", "active", "paused", "done"]
OPEN_DECISIONS_WARN = 12  # more open than this and the list is carrying ticket-level questions

DECISION_BAR = (
    "A decision is a direction choice: it changes the Goal or Scope, a ticket's plan, order or split, a contract "
    "between tickets, a product, design or architecture rule, or it needs someone else's sign-off. Record it with "
    "`tracker decide \"<title>\" --refs <ids> --question \"...\"` when it is raised, and resolve it with "
    "`tracker decide D-<n> --resolve \"...\" --by <who>` when it is answered, including an answer given in chat "
    "(a choice made on the spot is one command: `tracker decide \"<title>\" --resolve \"...\" --by <who>`). "
    "A choice inside one ticket's build (implementation detail, review fix, naming, styling) is not a decision: "
    "it goes in that ticket's Plan, `next` or PR.")
WAIT_RULE = (
    "Order and blockers live only in the waiting ticket's `depends_on`: when a ticket must wait on another ticket, "
    "a decision or something outside the tracker, or stops waiting, run `tracker wait <id> on|off <ids>` "
    "(`decide ... --blocks <ids>` for a decision). Do not write order or blockers as prose.")
START_RULE = (
    "A todo ticket can start when nothing blocks it, from the default branch. A ticket that waits only on tickets "
    "under way can start stacked on their work: from the branch that holds all of it (the branch they share, or the "
    "top of the stack their PRs form); when they are on separate branches, someone chooses one, or it waits until one "
    "merges. `tracker ready` and `tracker context <id>` name that branch, and `set <id> status=in-progress` says when "
    "the current branch does not contain it. Do not write the base as prose. The tracker never changes git.")
# The one statement of the tracker's isolation. Every session with a tracker gets it in its brief's protocol, every
# subagent in its SubagentStart line, and `tracker rules` prints it; the skill and the README point to it.
ISOLATION_RULE = (
    "The tracker is private to this work, isolated from the repos, PRs, issue trackers and other trackers it links "
    "to. Outside it (code, comments, commits, branch names, PR titles and bodies, issues, other trackers) write each "
    "fact in its own words, and cite only ids that exist there: a ticket's Issue id, a PR number, a URL. The "
    "tracker's own ids (D-n decisions, ticket ids that are not Issue ids), its name and its sections stay in it.")
TICKET_SECTIONS = ["Plan", "Carry forward", "Links"]  # required, in this order, and no others
DECISION_SECTIONS = ["Question", "Options", "Resolution"]  # Question required; Resolution required once closed
# Link labels every tracker accepts; a tracker adds its own in README frontmatter `labels`.
ISSUE, BLOCKER, EVIDENCE = "Issue", "Blocker", "Evidence"
DEFAULT_LABELS = ["Spec", "Design", "Doc", ISSUE, "PR", "Commit", BLOCKER, EVIDENCE, "Related"]
LABEL_RULES = {
    ISSUE: "the ticket's record in an issue tracker; its id (the first word of the link title, or a bare URL's last "
           "segment) also finds the ticket: `- Issue: [PROJ-12 Login page](url)`",
    BLOCKER: "something outside the tracker that the ticket waits on; names the id listed in `depends_on`",
    EVIDENCE: "a file in the tracker's evidence/ folder (measurements, runs, scripts), linked as `evidence/<name>`; "
              "`tracker attach` adds it",
}
EVIDENCE_DIR = "evidence"


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


def link_lines(items: list[Link], root: Path | None = None, titles: bool = False, width: int = 0) -> list[str]:
    """Link lines for the AI's context. `titles` keeps only each line's label and title: the short form. `width` (a
    brief) cuts each line to that many characters and leaves out the nested detail lines."""
    if titles:
        return [f"  {x.label}: {link_title(x).split(' — ')[0]}" for x in items]
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
    kind: str  # "ticket" | "decision"
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
    def links(self) -> list[Link]:
        return parse_links(section(self.body, "Links"))[0]

    @property
    def aliases(self) -> list[str]:
        """The ids of the ticket's Issue links: another name for the ticket."""
        return [link_ident(x) for x in self.links if x.label == ISSUE] if self.kind == "ticket" else []

    @property
    def stage(self) -> str:
        """A ticket's status; once in progress, overlaid by its PR's state. A todo ticket on a branch that holds a PR
        has not started, so the PR says nothing about it."""
        status = self.get("status", "todo")
        return PR_STAGE.get(self.get("pr_state"), status) if status == "in-progress" else status

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


_writes = 0  # tracker files this process wrote: a Tracker rebuilds its index after any write (Tracker.index)


def atomic_write(path: Path, text: str) -> None:
    global _writes
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    for wait in (0.05, 0.1, 0.2, 0.4, None):  # Windows refuses to replace a file while another process reads it
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)
    _writes += 1


def create(path: Path, text: str) -> None:
    """Write a new file; refuse one that exists, even one another session made a moment ago."""
    global _writes
    try:
        with open(path, "x") as f:
            f.write(text)
    except FileExistsError:
        die(f"{path} already exists")
    _writes += 1


class Busy(Exception):
    pass


LOCK_TIMEOUT_S = 20
_thread_lock = threading.Lock()  # the file lock is per process; this orders threads within one

if WINDOWS:  # a lock on the file's first byte
    import msvcrt

    def try_lock(f) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def try_lock(f) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def unlock(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)

_held = threading.local()  # lock depth per thread: reentrant within a thread, exclusive across threads


@contextmanager
def locked():
    """One writer at a time, across every session and worktree: held around each read-modify-write of tracker
    files. Reentrant within a process."""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    HOME.mkdir(parents=True, exist_ok=True)
    with _thread_lock, open(HOME / ".lock", "a") as f:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while not try_lock(f):
            if time.monotonic() > deadline:
                raise Busy(f"another tracker command has held the lock for {LOCK_TIMEOUT_S}s; try again")
            time.sleep(0.05)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            unlock(f)


class Tracker:
    def __init__(self, root: Path):
        self.root = root
        self.slug = root.name
        readme = root / "README.md"
        text = readme.read_text() if readme.exists() else ""
        lines, self.readme_body = split_frontmatter(text)
        self.meta, self.readme_problems = parse_meta(lines)
        self.readme_problems = frontmatter_problems(text) + self.readme_problems

    # Loaded on first use: a hook that only matches a branch never reads the decisions.
    @cached_property
    def tickets(self) -> list[Record]:
        return sorted((load_record(p, "ticket") for p in (self.root / "tickets").glob("*.md")),
                      key=lambda r: sort_key(r.id))

    @cached_property
    def decisions(self) -> list[Record]:
        return sorted((load_record(p, "decision") for p in (self.root / "decisions").glob("*.md")),
                      key=lambda r: sort_key(r.id))

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.slug)

    @property
    def context(self) -> list[Link]:
        return parse_links(section(self.readme_body, "Context"))[0]

    @property
    def labels(self) -> list[str]:
        extra = self.meta.get("labels") or []
        return list(dict.fromkeys(DEFAULT_LABELS + (extra if isinstance(extra, list) else [extra])))

    @property
    def schema(self) -> int:
        try:
            return int(self.meta.get("schema") or 0)
        except ValueError:
            return 0

    @property
    def repos(self) -> list[str]:
        repo = self.meta.get("repo") or []
        return repo if isinstance(repo, list) else [repo]

    def repo_of(self, t: Record) -> str:
        """The repo a ticket's branch and PR live in: its own `repo`, or the tracker's only one."""
        return str(t.get("repo") or (self.repos[0] if len(self.repos) == 1 else ""))

    def headline(self) -> str:
        """`title · status · owner`, the one line that names the work everywhere."""
        return " · ".join(str(x) for x in [self.title, self.meta.get("status"), self.meta.get("owner")] if x)

    @property
    def records(self) -> list[Record]:
        return self.tickets + self.decisions

    def index(self) -> dict:
        """Ids and Issue ids, then (as asked for) each ticket's dependencies and each record's waiting tickets. Built
        once per state of the files: any write through this module, or a record added to the lists, rebuilds it."""
        key = (_writes, len(self.tickets), len(self.decisions))
        if self.__dict__.get("_index_key") != key:
            ids, aliases = {}, {}
            for r in self.records:
                ids.setdefault(norm_id(r.id), r)
            for t in self.tickets:
                for a in t.aliases:
                    aliases.setdefault(norm_id(a), t)
            self._index, self._index_key = {"ids": ids, "aliases": aliases, "deps": {}}, key
        return self._index

    def lookup(self, ident: str) -> Record | None:
        """The ticket or decision with this id, or the ticket whose Issue link names it. Case and leading zeros do
        not count: `d-4` finds D-04."""
        index, want = self.index(), norm_id(ident)
        return index["ids"].get(want) or index["aliases"].get(want)

    def by_pr_or_branch(self, ident: str) -> Record | None:
        """The ticket whose PR is `#123` (`owner/name#123` when the tracker spans repos), or whose branch this is."""
        repo, _, num = ident.rpartition("#")
        if num.isdigit():
            hits = [t for t in self.tickets if str(t.get("pr")) == num
                    and (not repo or self.repo_of(t).lower() == repo.lower())]
            if len(hits) > 1:
                die(f"PR #{num} names {', '.join(t.id for t in hits)}: pass <owner/name>#{num}")
            return hits[0] if hits else None
        return next((t for t in self.tickets if t.get("branch") == ident), None)

    def find(self, ident: str) -> Record:
        """A record by id or Issue id; for a command's argument, also by PR (`#123`) or branch."""
        return (self.lookup(ident) or self.by_pr_or_branch(ident)
                or die(f"no ticket or decision '{ident}' in {self.slug}"))

    def tickets_on(self, branch: str) -> list[Record]:
        return [t for t in self.tickets if branch and t.get("branch") == branch]

    def open_decisions(self) -> list[Record]:
        return [d for d in self.decisions if d.get("status", "open") == "open"]

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
        """The started tickets on the branch that a ticket's open PR is based on: its PR cannot merge before theirs.
        Computed from the PR's `base`, so it ends when the PR is retargeted."""
        base = t.get("base")
        if not base or t.get("pr_state") not in ("open", "draft"):
            return []
        return [o for o in self.tickets if o is not t and o.get("branch") == base and o.get("status") != "todo"
                and same_repo(self.repo_of(o), self.repo_of(t))]

    def blockers(self, t: Record) -> list[Dep]:
        return [d for d in self.deps(t) if not d.done]

    def stackable(self, t: Record) -> bool:
        """Blocked only by tickets already in flight: its work can stack on their branches. An open decision or an
        external blocker still blocks it."""
        blockers = self.blockers(t)
        return bool(blockers) and all(d.kind == "ticket" and d.rec.stage in IN_FLIGHT for d in blockers)

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
        if t.stage != "todo" or (self.blockers(t) and not self.stackable(t)):
            return None
        under = [d.rec for d in self.blockers(t) if same_repo(self.repo_of(d.rec), self.repo_of(t))]
        base = next((b for b in under if all(self.holds(b, o) for o in under)), None)
        return Start(base, [] if base else under)

    def startable(self) -> list[tuple[Record, Start]]:
        """The todo tickets that can start now, each with where it starts."""
        return [(t, s) for t in self.tickets if (s := self.start_point(t))]

    def waiting_on(self, ident: str) -> list[Record]:
        """The tickets whose depends_on names this ticket or decision."""
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
        return [t for t in self.tickets if t.stage == "todo" and not self.blockers(t)]

    @cached_property
    def reviews(self) -> dict[str, dict]:
        """The review facts `sync` keeps per open PR (STATE_RULES `reviews`)."""
        reviews = self.raw_state().get("reviews", {})
        return reviews if isinstance(reviews, dict) else {}

    def review(self, t: Record) -> dict | None:
        """The review facts of a ticket's open PR; None without an open PR, or before `sync` read them."""
        if not t.get("pr") or t.get("pr_state") not in ("open", "draft"):
            return None
        return self.reviews.get(pr_key(self.repo_of(t), t.get("pr")))

    # -- machine state (.state.json): see STATE_RULES. Every read drops what no longer applies, so each write keeps
    # the file current.
    def raw_state(self) -> dict:
        try:
            state = json.loads((self.root / ".state.json").read_text())
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def state(self) -> dict:
        state, now = self.raw_state(), time.time()
        use = {}
        for key, ids in state.get("use", {}).items():
            ids = [i for i in ids if (r := self.lookup(i)) and r.kind == "ticket" and r.stage not in CLOSED_TICKET]
            if ids:
                use[key] = ids

        def current(part: str, key: str, entry) -> bool:
            repo, _, branch = key.rpartition(":")
            on = [t for t in self.tickets if t.get("branch") == branch and same_repo(self.repo_of(t), repo)]
            if any(t.stage not in CLOSED_TICKET for t in on) or any(k.endswith("@" + branch) for k in use):
                return True
            at = entry.get("at", 0) if isinstance(entry, dict) else 0
            return now - at < STATE_KEEP_DAYS * 86400 and (part == "synced" or not on)

        out = dict(state, use=use)
        for part in ("synced", "handoff"):
            out[part] = {k: v for k, v in state.get(part, {}).items() if current(part, k, v)}
        out["pr_match"] = {k: v for k, v in state.get("pr_match", {}).items() if now - v.get("at", 0) < PR_MATCH_TTL_S}
        return {k: v for k, v in out.items() if v != {}}

    def issue_due(self, now: float | None = None) -> list[Record]:
        """The tickets whose issue fields the model should read from their issue tracker (STATE_RULES["issues"])."""
        issues = self.raw_state().get("issues", {})
        read, asked = issues.get("read", {}), issues.get("requested", 0)
        now = time.time() if now is None else now
        return [t for t in self.tickets if t.aliases and (not read.get(t.id) or t.stage not in CLOSED_TICKET and (
            now - read[t.id] > ISSUE_STALE_S or read[t.id] < asked))]

    def save_state(self, state: dict) -> None:
        atomic_write(self.root / ".state.json", json.dumps(state, indent=2) + "\n")

    def data_files(self) -> list[Path]:
        """Every file that holds the tracker's facts: a change to one is a change the viewer and the watcher show."""
        return [self.root / "README.md", self.root / "log.md", self.root / ".state.json",
                *sorted((self.root / "tickets").glob("*.md")), *sorted((self.root / "decisions").glob("*.md"))]


def files_hash(files: list[Path], extra: str = "") -> str:
    """Changes when any of the files (or `extra`) does: their names, times and sizes. A missing file counts as none."""
    stats = []
    for p in files:
        try:
            s = p.stat()
        except OSError:
            continue
        stats.append(f"{p.name}:{s.st_mtime_ns}:{s.st_size}")
    return hashlib.sha1(("|".join(stats) + extra).encode()).hexdigest()[:12]


def sort_key(ident: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", ident)]


def norm_id(ident: str) -> tuple:
    """An id as lookups compare it: case and leading zeros do not count."""
    return tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", ident.strip()))


def id_list(values: list[str] | None) -> list[str]:
    """Ids given as one comma list, as separate words, or both: `--ref SS-2,D-03` = `--ref SS-2 D-03`."""
    return csv(",".join(values or []))


def all_trackers() -> list[Tracker]:
    if not HOME.exists():
        return []
    return [Tracker(p) for p in sorted(HOME.iterdir()) if p.is_dir() and (p / "README.md").exists()]


def tracker_at(slug: str) -> Tracker | None:
    """The tracker named `slug`, if it exists. A slug is one folder name in HOME, never a path."""
    slug = str(slug or "")
    return Tracker(HOME / slug) if SAFE_NAME.fullmatch(slug) and (HOME / slug / "README.md").exists() else None


RESOLUTION_LINE = re.compile(r"^(?:- )?\d{4}-\d{2}-\d{2}\b")


def resolution(d: Record) -> str:
    """A closed decision's answer: the latest dated `YYYY-MM-DD (who): answer` line of its ## Resolution, or, when a
    person wrote it without one, its first line (detail lines follow the answer)."""
    lines = [ln.strip() for ln in strip_comments(section(d.body, "Resolution")).splitlines() if ln.strip()]
    dated = [ln for ln in lines if RESOLUTION_LINE.match(ln)]
    return (dated or lines or [""])[-1 if dated else 0].removeprefix("- ")


# ---------------------------------------------------------------- dependencies
# A ticket's `depends_on` is the one place an order or a blocker is written, always on the ticket that waits. Each
# item is a ticket (satisfied once it is merged, done or dropped), a decision (satisfied once closed), or any other
# id: an external blocker, satisfied only when it is removed, and named by a `- Blocker:` line in the ticket's ## Links
# that says where it is and why it blocks. An open PR based on another ticket's branch also waits on that ticket: that
# comes from the PR's `base`, not depends_on. Blockers, "unblocks", steps, the critical path and the ready list are all
# computed.

DECISION_ID = re.compile(r"D-\d+", re.I)
STARTED = {"in-progress", "in-review", "merged", "done"}  # stages
IN_FLIGHT = {"in-progress", "in-review"}  # stages: started, not yet closed


@dataclass
class Dep:
    ident: str  # the record's id (a key in depends_on resolves to it), or the external id as written
    rec: Record | None = None  # None for an external blocker
    link: Link | None = None  # an external blocker's line in ## Links

    @property
    def kind(self) -> str:
        return self.rec.kind if self.rec else "external"

    @property
    def done(self) -> bool:
        if not self.rec:
            return False
        if self.rec.kind == "ticket":
            return self.rec.stage in CLOSED_TICKET
        return self.rec.get("status") == "closed"

    def describe(self) -> str:
        """`T-5 in-review`, `D-10 open: <title>`, `X external: <why>`, for the AI's plain-text context."""
        if not self.rec:
            return f"{self.ident} external" + (f": {plain_link(self.link.text)}" if self.link else "")
        status = self.rec.stage if self.rec.kind == "ticket" else self.rec.get("status", "")
        if self.done:
            return f"{self.ident} {status} ✓"
        return f"{self.ident} {status}" + (f": {self.rec.get('title')}" if self.kind == "decision" else "")


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


def blocker_link(t: Record, ident: str) -> Link | None:
    """The `- Blocker:` line in a ticket's ## Links that names an external blocker."""
    return next((x for x in t.links if x.label == BLOCKER and names(x, ident)), None)


@dataclass
class Sequence:
    step: dict[str, int]  # per ticket: 1 + the longest chain of tickets it waits on
    critical: list[str]  # the longest chain of unfinished tickets, first to last
    cycles: list[list[str]]


def sequence(tr: Tracker) -> Sequence:
    edges = {t.id: [d.ident for d in tr.deps(t) if d.kind == "ticket" and d.ident != t.id] for t in tr.tickets}
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
    return Sequence(step, [] if cycles else longest_open_chain(tr, edges), cycles)


def longest_open_chain(tr: Tracker, edges: dict[str, list[str]]) -> list[str]:
    unfinished = {t.id for t in tr.tickets if t.stage not in CLOSED_TICKET}
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
    """A PR's key in .state.json `reviews`: `owner/name#n`."""
    return f"{repo.lower()}#{pr}"


# ---------------------------------------------------------------- whose move
# Whose turn a ticket under way waits on: this side (the user and the AI) or the people or thing named. Computed from
# the review facts `sync` reads from the PR, and the ticket's open decisions and external blockers; nothing writes it.

YOU = "you"
MOVE_RULE = (
    "A ticket under way has a move: whose turn it waits on, computed, never written. In order: merge conflicts, "
    "failing checks or requested changes not re-requested are yours; a requested review is the reviewer's; an open "
    "decision it waits on is its owner's; an external blocker is that blocker's; then unresolved review threads, an "
    "approval (CI's while its checks run), a draft or a PR with no review requested are yours. `next` holds your own "
    "next action: do not write a wait on a reviewer into it.")


@dataclass
class Move:
    who: str  # YOU, or the reviewers, the decision's owner or the blocker's id
    what: str = ""  # why it is their move, as a state: "review requested", "checks failing"
    rank: int = 0  # its rule's place in MOVE_RULE: the more urgent first

    @property
    def mine(self) -> bool:
        return self.who == YOU

    def text(self, sep: str = ": ") -> str:
        return self.who + (sep + self.what if self.what else "")


def whose_move(tr: Tracker, t: Record) -> Move | None:
    """The move of a ticket under way (MOVE_RULE); None for any other ticket, and for an open PR whose review facts
    `sync` has not read."""
    if t.stage not in IN_FLIGHT:
        return None
    r = tr.review(t)
    if r is None and t.get("pr_state") in ("open", "draft"):
        return None
    r = r or {}
    threads = r.get("threads", 0)
    open_threads = f"{threads} unresolved review thread{'s' * (threads != 1)}"
    requested = r.get("requested", [])
    approved = "approved" + (f" by {', '.join(r['approved'])}" if r.get("approved") else "") \
        if r.get("decision") == "approved" else ""
    if r.get("merge") == "dirty":
        return Move(YOU, "merge conflicts", 1)
    if r.get("checks") in ("failure", "error"):
        return Move(YOU, "checks failing", 2)
    if r.get("decision") == "changes_requested" and not requested:
        by = ", ".join(r.get("changes", [])) or "a reviewer"
        if r.get("reviewed") and r.get("pushed", 0) > r["reviewed"]:
            return Move(YOU, f"pushed since {by} requested changes; review not re-requested", 3)
        return Move(YOU, f"changes requested by {by}" + (f" ({open_threads})" if threads else ""), 3)
    if requested:
        return Move(", ".join(requested), "review requested", 4)
    for d in tr.blockers(t):
        if d.kind == "decision":
            owner = str(d.rec.get("owner") or "")
            return Move(owner or d.ident, f"{d.ident} open" if owner else "open decision", 5)
        if d.kind == "external":
            note = link_title(d.link).partition(" — ")[2] if d.link else ""
            return Move(d.ident, short(note, 60) if note else "", 6)
    if threads:
        return Move(YOU, f"{approved}; {open_threads}" if approved else open_threads, 7)
    if approved:
        if r.get("checks") in ("pending", "expected"):
            return Move("CI", "checks running", 8)
        state = {"behind": "branch behind its base", "blocked": "GitHub blocks the merge"}.get(r.get("merge", ""),
                                                                                             "ready to merge")
        return Move(YOU, f"{approved}; {state}", 8)
    if r.get("draft"):
        return Move(YOU, "draft", 9)
    if r:
        return Move(YOU, "no review requested", 9)
    return Move(YOU, "PR closed" if t.get("pr_state") == "closed" else "no PR yet", 10)


def unblocked(tr: Tracker, blocked: set[str]) -> list[str]:
    """A line naming the unfinished tickets in `blocked` that nothing blocks now."""
    free = [t.id for t in tr.tickets if t.id in blocked and not tr.blockers(t) and t.stage not in CLOSED_TICKET]
    return [f"nothing blocks {', '.join(free)} now (to start one: `tracker set <id> status=in-progress`)"] \
        if free else []


def append_log(tr: Tracker, msg: str, refs: list[str]) -> str:
    line = f"- {today()}" + (f" [{' '.join(refs)}]" if refs else "") + f" {' '.join(msg.split())}"
    with open(tr.root / "log.md", "a") as f:
        f.write(line + "\n")
    return line


def short(text: str, n: int = 90) -> str:
    """One line of at most n characters: a log line names a fact whose full text lives in its own file."""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a ticket id, tracker slug or session id: also a file name


# ---------------------------------------------------------------- body edits
# Every change to a record's body goes through one of these: `tracker add`, `drop`, `decide --resolve`, `wait` and
# `attach` change one section of a file without a hand edit, and `migrate` relabels lines.

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
