"""The tracker's data: the contract's constants (keys, statuses, sections, labels), records, the Tracker,
dependencies, body edits and the write lock. The text format itself is markdown's."""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from .markdown import (BULLET, bullets, format_value, frontmatter_problems, link_ident, parse_links, parse_meta,
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

@dataclass(frozen=True)
class Stage:
    """What a ticket's stage says of its work. Code asks a record (`Record.todo`, `in_flight`, `started`, `closed`,
    `dropped`); it names a stage only where that one stage matters (in progress against in review)."""
    todo: bool = False  # its work has yet to start
    in_flight: bool = False  # under way: started, not yet closed
    closed: bool = False
    dropped: bool = False  # closed without its work done

    @property
    def started(self) -> bool:
        return self.in_flight or self.closed and not self.dropped


NO_STAGE = Stage()  # of an unknown stage (`check` reports it), and of a record that is no ticket
# A ticket's stages, in the order the views count and sort them (`Record.stage` says which one a ticket is in).
STAGES = {"todo": Stage(todo=True), "in-progress": Stage(in_flight=True), "in-review": Stage(in_flight=True),
          "merged": Stage(closed=True), "done": Stage(closed=True), "dropped": Stage(closed=True, dropped=True)}
PR_STAGE = {"draft": "in-progress", "open": "in-review", "merged": "merged"}  # pr_state -> stage of started work
OPEN_PR = {"draft", "open"}  # pr_state values of a PR not yet merged or closed
OPEN_STAGES = {s for s, x in STAGES.items() if not x.closed}


@dataclass(frozen=True)
class Statuses:
    """The words one kind of file's `status` holds, the first a new file's, and those that close it. Each kind keeps
    its own words; whether a record is closed is `Record.closed`, never a comparison with a word."""
    values: tuple[str, ...]
    closed: frozenset[str]


TICKET_STATUSES = ("todo", "in-progress", "done", "dropped")  # what you set; the PR adds the other stages
STATUSES = {
    "ticket": Statuses(TICKET_STATUSES, frozenset(s for s in TICKET_STATUSES if STAGES[s].closed)),
    "decision": Statuses(("open", "closed"), frozenset({"closed"})),
    "action": Statuses(("open", "done", "dropped"), frozenset({"done", "dropped"})),
    "tracker": Statuses(("planning", "active", "paused", "done"), frozenset({"done"})),  # the work's
}


# ---------------------------------------------------------------- scales
# A scale is a ticket key that holds a level, the same whatever the issue tracker: an issue's own value is mapped onto
# it when it is read (SCALE_RULE), so levels compare, filter and sort across issue trackers. A level is a rank, not an
# amount (two XS are not an S): a sum, an average or a score adds the scale's weights (`weight`), never levels.
# A new scale is one SCALES entry: its ticket key, the CLI flags (`new`, `set`, `issue`), `check`, `tracker rules` and
# the viewer's column follow from it.

@dataclass(frozen=True)
class Scale:
    meaning: str  # what the key says of a ticket; `tracker rules` gives it with the key
    levels: range
    names: tuple[str, ...]  # each level's name in the views, in order
    weights: tuple[float, ...]  # each level's amount in `unit`, in order: what a sum adds
    unit: str  # what a weight counts
    ends: str  # what its ends mean
    rule: str  # how a level is chosen; `tracker rules` prints it

    @property
    def span(self) -> str:
        return f"{self.levels[0]}-{self.levels[-1]}"

    def parse(self, value) -> int | None:
        """A stored value as a level; None for none, or a value `check` refuses."""
        text = "" if value is None else str(value).strip()
        return int(text) if text.isdigit() and int(text) in self.levels else None

    def name(self, n: int) -> str:
        return self.names[self.levels.index(n)]

    def weight(self, n: int) -> float:
        return self.weights[self.levels.index(n)]


SCALES = {
    "priority": Scale("how urgent the ticket is", range(5), ("P0", "P1", "P2", "P3", "P4"), (8, 5, 3, 2, 1),
                      "urgency points", "0 is the most urgent, 4 the least",
                      "0 (most urgent) to 4 (least): Urgent, Highest or Blocker 0, High 1, Medium or Normal 2, Low 3, "
                      "Lowest or Trivial 4, and P0-P4 their digit. Yours: from how urgent the work is"),
    # The weights are the amounts the rule names, in days of work: XS a quarter day, an XL 8 days at least.
    "size": Scale("how much work the ticket is", range(1, 6), ("XS", "S", "M", "L", "XL"), (0.25, 1, 3, 5, 8),
                  "days of work", "1 is XS, 5 XL",
                  "1 (XS) to 5 (XL), as an amount of work: XS an hour or two, S about a day, M a few days, L about a "
                  "week, XL more. A T-shirt size is its letter; points take their place on the issue tracker's "
                  "sequence (Fibonacci 1, 2, 3, 5, 8+; powers of two 1, 2, 4, 8, 16+); a time estimate goes by its "
                  "amount. Yours: from how much work its Plan is"),
}
SCALE_RULE = (
    "a ticket with an Issue link takes its issue's value, mapped by what it means on that issue tracker's own scale, "
    "never by a raw number its API gives. A value that means none (No priority, unestimated) leaves the key empty; "
    "one past an end takes that end. Record it with `tracker issue <id> --priority <n> --size <n>`. A ticket with "
    "no issue value (no Issue link, or an issue tracker without the field) gets yours: `tracker new --priority <n> "
    "--size <n>` or `tracker set <id> priority=<n> size=<n>`")
OWN_VALUE_RULE = (
    "a value you set by your own judgement (priority, size) is your best estimate; leave it empty when you do not "
    "know it or cannot estimate it reliably. A value from an issue tracker or the user is never a guess")


def level(t: Record, key: str) -> int | None:
    """A ticket's level on SCALES[key]; None when it has none, or a value `check` refuses."""
    return SCALES[key].parse(t.get(key))


def level_name(t: Record, key: str) -> str:
    """A ticket's level by its name (P1, XS); a value `check` refuses as it is; "" for none."""
    return show_value(key, t.get(key))


def weight(t: Record, key: str) -> float | None:
    """A ticket's level as its scale's amount (in the scale's unit), which adds up; None when it has no level."""
    n = level(t, key)
    return SCALES[key].weight(n) if n is not None else None


# Every frontmatter key per file: what writes it ("set" = `tracker set`, or the command that owns it) and what it holds.
DUE = "the day it should be done by; optional: only a day the user or an issue tracker gave, never your own"
UPDATED = "the last change through the CLI"
KEYS = {
    "ticket": {
        "id": ("new", "the ticket's id; also its file name"),
        "title": ("set", "short name of the ticket"),
        "status": ("set", "the work status; once in-progress, the stage shown is this overlaid by the PR"),
        "group": ("set", "a free label that groups tickets in the views; not an order"),
        "branch": ("set", "the git branch the ticket is built on; finds the ticket and its PR. A branch holds any "
                          "number of tickets. `set status=in-progress` records the current branch when empty"),
        "repo": ("set", "owner/name of the ticket's repo, when the tracker spans several"),
        "depends_on": ("wait", "what the ticket waits on: ticket, decision, action or external ids; the only "
                               "record of order and blockers"),
        "next": ("set", "one concrete next action, true as of now; cleared when the ticket closes"),
        "summary": ("set", "one line: what the ticket delivered or why it was dropped; shown once it is closed"),
        **{key: ("set", f"{scale.meaning}; see {key.capitalize()}") for key, scale in SCALES.items()},
        "due": ("set", DUE),
        "created_at": ("auto", "when `tracker new` added it"),
        "updated_at": ("auto", UPDATED),
        "started_at": ("auto", "when `set status=in-progress` first started it; none for one started before 0.29"),
        "closed_at": ("auto", "when its status closed it (done or dropped); a merged PR's time is merged_at"),
        "pr": ("sync", "the PR number"),
        "pr_state": ("sync", "draft|open|merged|closed, from the PR"),
        "base": ("sync", "the PR's base branch; while the PR is open, a base that is other tickets' branch makes "
                         "this ticket wait on them (computed; depends_on does not change)"),
        "merged_at": ("sync", "when the PR merged"),
        "issue_created_at": ("issue", "when the ticket's issue was created in its issue tracker"),
    },
    "decision": {
        "id": ("decide", "D-<n>; also the file name"),
        "title": ("set", "short name of the decision"),
        "status": ("decide", "open until `decide --resolve` closes it; the answer is ## Resolution"),
        "refs": ("decide", "tickets it touches but does not block; a block is the ticket's depends_on"),
        "owner": ("set", "who must decide"),
        "due": ("set", DUE),
        "created_at": ("auto", "when it was opened"),
        "updated_at": ("auto", UPDATED),
        "closed_at": ("auto", "when `decide --resolve` closed it"),
    },
    "action": {
        "id": ("act", "A-<n>; also the file name"),
        "title": ("act", "what the user must do, and with whom"),
        "status": ("act", "open until `act --done` or `act --drop` closes it"),
        "refs": ("act", "tickets and decisions it concerns but does not block; a block is the ticket's depends_on"),
        "due": ("act", DUE),
        "created_at": ("auto", "when it was added"),
        "updated_at": ("auto", UPDATED),
        "closed_at": ("auto", "when `act --done` or `act --drop` closed it"),
    },
    "tracker": {
        "title": ("set", "name of the work"),
        "repo": ("set", "GitHub owner/name, or a list when the work spans repos; enables `sync`"),
        "status": ("set", "the work's status"),
        "owner": ("set", "who owns the work"),
        "created_at": ("auto", "when the tracker was made"),
        "labels": ("set", "this tracker's own link labels, added to the defaults"),
        "schema": ("auto", "the tracker's format version; `tracker migrate` brings an older one up to date"),
    },
}
SCHEMA = 3  # the tracker format this code writes; README `schema` names a tracker's (none: older than 1)
# Keys an older format named otherwise, old: new; `migrate` renames them
RENAMED_KEYS = {"ticket": {"updated": "updated_at", "issue_created": "issue_created_at"},
                "decision": {"opened": "created_at", "updated": "updated_at"},
                "action": {"opened": "created_at", "updated": "updated_at"},
                "tracker": {"created": "created_at"}}
# `migrate` removes them, or renames them
RETIRED_KEYS = {kind: removed | set(RENAMED_KEYS[kind]) for kind, removed in
                {"ticket": {"slice", "key"}, "decision": {"resolved"}, "action": set(), "tracker": set()}.items()}
# `tracker issue`'s flags: the ticket key each records from the ticket's issue
ISSUE_FIELDS = {**{key: key for key in SCALES}, "due": "due", "created": "issue_created_at"}
OWNER_HINT = {"new": "fixed at `tracker new`", "decide": "use `tracker decide`", "wait": "use `tracker wait`",
              "act": "use `tracker act`", "sync": "`tracker sync` writes it from the PR",
              "auto": "the tracker writes it", "issue": "`tracker issue` writes it from the ticket's issue tracker"}
# Machine state in .state.json, never in frontmatter: what `tracker rules` says about it. A branch's entries are keyed
# by state_key, so a tracker can span repos. Each key here names a part for `tracker rules`: the mark is stored as
# `synced`, and `cleanup` is no key.
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
    "prs": "per open PR (`owner/name#n`), what `sync` last read of its reviews, checks and merge state, in the "
           "tracker's words (PR_FACTS); each sync replaces them. A ticket's move is computed from them",
    "issues": "`read`: per ticket, when `tracker issue` last recorded its issue's fields; `requested`: when the "
              "viewer's Refresh asked for them again. A ticket with an Issue link is to read when open and never read "
              "or read before the request; when closed, only when never read and it has a "
              "`started_at` (its wait time needs the issue's creation time); the brief and the prompt hook list the "
              "ones to read for the model, which reads them with the issue tracker's tool",
    "cleanup": f"while a branch has an unfinished ticket its entries stay. Otherwise a handoff goes once the "
               f"branch's tickets are closed, and a mark, or a handoff on a branch no ticket is on, {STATE_KEEP_DAYS} "
               f"days after it was set. `use` keeps only unfinished tickets",
}


# ---------------------------------------------------------------- values
# A key's name gives its value's form (value_form). Each form (FORMS) reads a value a command is given (`read_value`),
# holds a stored value to it (`valid_value`, for `check`) and shows it (`show_value`); a new form is one Form subclass.
# A time the tracker writes is UTC to the second; a `_at` key may hold a date alone, written before the tracker kept
# the time: it gives no span.

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DAY_KEYS = {"due"}
LIST_KEYS = {"depends_on", "refs", "labels"}
LIST_OR_ONE = {"repo"}  # one value, or a list when the work spans repos


class Form:
    """Text: any value, as given. Each other form overrides what differs."""

    def describe(self, key: str) -> str:
        """What its values are, for `check` and `tracker rules`."""
        return "text"

    def fits(self, key: str, value) -> bool:
        """A stored value, not empty, is of this form."""
        return True

    def read(self, key: str, text: str):
        """A value a command is given, not empty, as it is stored; ValueError, saying why, when it does not fit."""
        return text

    def show(self, key: str, value) -> str:
        return str(value)


class TimeForm(Form):
    def describe(self, key: str) -> str:
        return "a time, UTC to the second (YYYY-MM-DDTHH:MM:SSZ), or a date alone when written before the time was kept"

    def fits(self, key: str, value) -> bool:
        return utc_seconds(value) is not None or parse_day(value) is not None

    def read(self, key: str, text: str) -> str:
        """An ISO 8601 time with its zone, as UTC to the second; refused without a zone, which would be a guess."""
        try:
            at = dt.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"'{text}' is not an ISO 8601 time (2026-10-01T09:30:00Z)") from None
        if at.tzinfo is None:
            raise ValueError(f"'{text}' needs a time zone (Z or +10:00): the issue tracker gives one")
        return at.astimezone(dt.timezone.utc).strftime(TIME_FORMAT)

    def show(self, key: str, value) -> str:
        return day_text(value)


class DayForm(Form):
    def describe(self, key: str) -> str:
        return "a day (YYYY-MM-DD)"

    def fits(self, key: str, value) -> bool:
        return parse_day(value) is not None

    def read(self, key: str, text: str) -> str:
        """`none` clears it."""
        if text.lower() == "none":
            return ""
        day = parse_day(text)
        if not day:
            raise ValueError(f"{key} '{text}' is not a day: YYYY-MM-DD (or `none` to remove it)")
        return day.isoformat()


class LevelForm(Form):
    """A level on the key's scale (SCALES), stored as its number and shown by its name."""

    def describe(self, key: str) -> str:
        return SCALES[key].span

    def fits(self, key: str, value) -> bool:
        return SCALES[key].parse(value) is not None

    def read(self, key: str, text: str) -> str:
        scale = SCALES[key]
        if scale.parse(text) is None:
            raise ValueError(f"{key} '{text}' is not {scale.span}: {scale.ends} (`tracker rules` says how an issue "
                             f"tracker's values map onto it)")
        return str(int(text))

    def show(self, key: str, value) -> str:
        n = SCALES[key].parse(value)
        return SCALES[key].name(n) if n is not None else str(value)


class ListForm(Form):
    """A comma list given; a LIST_OR_ONE key keeps one value as it is."""

    def describe(self, key: str) -> str:
        return "a list: [a, b]"

    def read(self, key: str, text: str):
        items = csv(text)
        return text if key in LIST_OR_ONE and len(items) == 1 else items

    def show(self, key: str, value) -> str:
        return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


FORMS = {"time": TimeForm(), "day": DayForm(), "level": LevelForm(), "list": ListForm(), "": Form()}


def value_form(key: str) -> str:
    """The form of a key's value (FORMS): "time", "day", "level" (a SCALES key), "list", or "" for text."""
    return ("time" if key.endswith("_at") else "day" if key in DAY_KEYS else "level" if key in SCALES
            else "list" if key in LIST_KEYS | LIST_OR_ONE else "")


def read_value(key: str, text: str):
    """A value a command is given for a key, as its form stores it: a level as its number, a day as YYYY-MM-DD, a time
    as UTC. Empty clears the key (a list key's is an empty list). Refused, saying why, when it does not fit."""
    text, form = text.strip(), FORMS[value_form(key)]
    if not text:
        return [] if key in LIST_KEYS else ""
    try:
        return form.read(key, text)
    except ValueError as exc:
        die(str(exc))


def valid_value(key: str, value) -> bool:
    """A stored value fits its key's form; an empty one always does."""
    return value in ("", None) or FORMS[value_form(key)].fits(key, value)


def show_value(key: str, value) -> str:
    """A stored value as the views show it: a time as its local day, a level by its name, a list joined."""
    return FORMS[value_form(key)].show(key, value) if value not in ("", None) else ""


def utc_now() -> str:
    return time.strftime(TIME_FORMAT, time.gmtime())


def utc_seconds(text) -> int | None:
    """A time as epoch seconds; None for anything else, a date alone included."""
    try:
        return calendar.timegm(time.strptime(str(text), TIME_FORMAT))
    except ValueError:
        return None


def parse_day(text) -> dt.date | None:
    """A `YYYY-MM-DD` day; None for anything else."""
    text = str(text or "").strip()
    try:
        return dt.date.fromisoformat(text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
    except ValueError:
        return None


def day_of(text) -> dt.date | None:
    """A time's local day, or a date alone as it is; None for anything else."""
    seconds = utc_seconds(text)
    return dt.date.fromtimestamp(seconds) if seconds is not None else parse_day(text)


def day_text(text) -> str:
    """A time as its local day (YYYY-MM-DD); any other value as it is."""
    day = day_of(text)
    return day.isoformat() if day else str(text or "")


def days_since(text) -> int:
    """Whole days from a time's or a date's day to today; 0 for anything else."""
    day = day_of(text)
    return (dt.date.today() - day).days if day else 0


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
STALE_ACTION_DAYS = 7
STALE_TICKET_DAYS = 7
SYNC_MIN_INTERVAL_S = 600
PR_MATCH_TTL_S = 600  # how long a branch's PR, once asked, is not asked again


def dated(text: str) -> str:
    """`<today>: text`; a text that starts with a date keeps its own."""
    return text if re.match(r"\d{4}-\d{2}-\d{2}\b", text) else f"{today()}: {text}"


def today() -> str:
    return dt.date.today().isoformat()


def die(msg: str, code: int = 2) -> None:
    print(f"tracker: {msg}", file=sys.stderr)
    sys.exit(code)


def spawn(*args: str) -> None:
    """Start `tracker <args>` in the background: it outlives this process and writes to no terminal."""
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
              else {"start_new_session": True})
    subprocess.Popen([*CLI, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **detach)


def to_trash(path: Path) -> None:
    """Move a file or folder to the system's trash (macOS Trash, Windows Recycle Bin, the freedesktop trash
    elsewhere), from where the user can put it back. Raises OSError, and leaves the path, when it cannot."""
    if WINDOWS:
        windows_trash(path)
    elif sys.platform == "darwin":
        mac_trash(path)
    else:
        xdg_trash(path)


def mac_trash(path: Path) -> None:
    """`trash` (macOS 15 and later); before it, Finder, which asks the user once to let this app control it."""
    cmd = (["/usr/bin/trash", str(path)] if Path("/usr/bin/trash").exists() else
           ["osascript", "-e", "on run argv", "-e",
            'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)', "-e", "end run", str(path)])
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{cmd[0]} did not finish") from exc
    if done.returncode or path.exists():
        raise OSError(f"could not move {path} to the Trash: {done.stderr.strip() or done.stdout.strip()}")


def windows_trash(path: Path) -> None:
    """SHFileOperationW with FOF_ALLOWUNDO: the Recycle Bin, with no dialog."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    fo_delete, flags = 3, 0x40 | 0x10 | 0x4 | 0x400  # FOF_ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
    op = SHFILEOPSTRUCTW(None, fo_delete, str(path.resolve()) + "\0", None, flags, False, None, None)  # 2 NULs end it
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"could not move {path} to the Recycle Bin (error {code:#x})")


def xdg_trash(path: Path) -> None:
    """The freedesktop.org home trash: the file in Trash/files, and a .trashinfo in Trash/info that says where it
    came from, under a name no other item there has."""
    import urllib.parse
    trash = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"
    (trash / "files").mkdir(parents=True, exist_ok=True)
    (trash / "info").mkdir(exist_ok=True)
    n = 1
    while True:
        name = path.name if n == 1 else f"{path.name}.{n}"
        info = trash / "info" / f"{name}.trashinfo"
        try:
            fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1
            continue
        if not (trash / "files" / name).exists():
            break
        os.close(fd)
        info.unlink()
        n += 1
    with os.fdopen(fd, "w") as f:
        f.write(f"[Trash Info]\nPath={urllib.parse.quote(str(path.absolute()))}\n"
                f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        shutil.move(str(path), str(trash / "files" / name))  # a copy when the trash is on another disk
    except OSError:
        info.unlink(missing_ok=True)
        raise


# The longest text a command takes, in characters: a guard against a paragraph where a line belongs, generous so that
# a line a little long is not refused and written again. Enforced when written; `check` leaves older text alone.
TEXT_MAX = {  # kind: (characters, what it is, where the rest goes)
    "next": (400, "next", "one concrete action; the detail goes in the Plan or the PR"),
    "summary": (400, "summary", "one line; the detail goes in the PR"),
    "log": (400, "log line", "what changed and why, in short; the detail goes in the PR, the commits or the ticket"),
    "carry": (600, "Carry forward bullet", "one fact per bullet: split it into more"),
    "action": (200, "action", "one line: what to do and with whom; the detail goes in `--note`"),
    "note": (400, "action note", "one fact per note: pass `--note` again for the next (the context, then the reply)"),
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
# An optional README section: this work's standing instructions for the agent, which every brief prints in full.
README_INSTRUCTIONS = "Instructions"
README_KEYS = ["title", "repo", "status", "owner", "created_at"]  # required frontmatter; repo may be empty
SCOPE_PARTS = ["In", "Out"]  # ### subsections of ## Scope
OPEN_DECISIONS_WARN = 12  # more open than this and the list is carrying ticket-level questions

DECISION_BAR = (
    "A decision is a direction choice: it changes the Goal or Scope, a ticket's plan, order or split, a contract "
    "between tickets, a product, design or architecture rule, or it needs someone else's sign-off. Record it with "
    "`tracker decide \"<title>\" --refs <ids> --question \"...\"` when it is raised, and resolve it with "
    "`tracker decide D-<n> --resolve \"...\" --by <who>` when it is answered, including an answer given in chat "
    "(a choice made on the spot is one command: `tracker decide \"<title>\" --resolve \"...\" --by <who>`). "
    "A choice inside one ticket's build (implementation detail, review fix, naming, styling) is not a decision: "
    "it goes in that ticket's Plan, `next` or PR.")
ACTION_BAR = (
    "An action is a task for the user that you cannot or should not do: talk to or follow up with a person, get an "
    "access or a sign-off, a step on a system you have no access to. It is never your own next step (`next`), a "
    "wait the PR shows (a review, checks), or a choice (a decision). Ask the user before you add one, unless they "
    "asked for it: `tracker act \"<what to do, with whom>\" --refs <ids>`. When the user says it is done, or no "
    "longer needed, close it: `tracker act A-<n> --done` (or `--drop`), with `--note \"<outcome>\"` when it has one. "
    "A note is one fact (the context when you add it, the reply when it comes): `--note` repeats. When the user "
    "gives a day it is due by, pass `--due YYYY-MM-DD`; never set one they did not give.")
WAIT_RULE = (
    "Order and blockers live only in the waiting ticket's `depends_on`: when a ticket must wait on another ticket, "
    "a decision, a user's action or something outside the tracker, or stops waiting, run `tracker wait <id> on|off "
    "<ids>` (`decide` or `act ... --blocks <ids>` for a new decision or action). Do not write order or blockers as "
    "prose.")
START_RULE = (
    "A todo ticket can start when nothing blocks it, from the default branch. A ticket that waits only on tickets "
    "under way can start stacked on their work: from the branch that holds all of it (the branch they share, or the "
    "top of the stack their PRs form); when they are on separate branches, someone chooses one, or it waits until one "
    "merges. `tracker ready` and `tracker context <id>` name that branch, and `set <id> status=in-progress` says when "
    "the current branch does not contain it. Do not write the base as prose. The tracker never changes git.")
# The one statement of the tracker's isolation. Every session with a tracker gets it in its brief's protocol, every
# subagent in its SubagentStart line, and `tracker rules` prints it; the skill points to it.
ISOLATION_RULE = (
    "The tracker is private to this work, isolated from the repos, PRs, issue trackers and other trackers it links "
    "to. Outside it (code, comments, commits, branch names, PR titles and bodies, issues, other trackers) write each "
    "fact in its own words, and cite only ids that exist there: a ticket's Issue id, a PR number, a URL. The "
    "tracker's own ids (D-n decisions, ticket ids that are not Issue ids), its name and its sections stay in it.")
# What a `step` message holds, said wherever a text asks for a step: the brief's protocol and its lag line, and the
# hooks' request after commits (next_request). The model writes the message right after reading one of them.
STEP_MESSAGE = ("a message only for what the commits do not say (a result, a measurement, why), never what they or "
                "the PR hold: the work a commit names, a push, a merge, a review round, a test run")


@dataclass(frozen=True)
class Kind:
    """A kind of record: one file per record in its folder, made from templates/<kind>.md. Its keys are KEYS[kind]
    and its status words STATUSES[kind]; code that serves every kind reads this table, so a new kind is one entry
    here, in KEYS and in STATUSES, and its command."""
    folder: str
    prefix: str  # the letter of its numbered ids (`D`: D-01, D-02); "" for a ticket, whose id is given
    command: str  # the command that adds one; for a numbered kind, also the one that closes it and takes its notes
    sections: tuple[str, ...] = ()  # its ## sections, in this order and no others; none: its body is notes
    required: tuple[str, ...] = ()  # the sections it must have
    closing: str = ""  # the section a closed one must have: its answer
    refs: frozenset[str] = frozenset()  # the kinds its `refs` may name
    rule: str = ""  # what `tracker rules` says of its file beyond its sections


KINDS = {
    "ticket": Kind("tickets", "", "new", ("Plan", "Carry forward", "Links"), ("Plan", "Carry forward", "Links"),
                   rule=f"Carry forward ≤ {MERGED_CARRY_FORWARD_MAX} bullets once merged or done. Ids: letters, "
                        "digits, `.`, `_`, `-`."),
    "decision": Kind("decisions", "D", "decide", ("Question", "Options", "Resolution"), ("Question",), "Resolution",
                     frozenset({"ticket"}), "## Resolution holds one dated line per answer, `YYYY-MM-DD (who): "
                                            "answer`; the latest holds."),
    "action": Kind("actions", "A", "act", refs=frozenset({"ticket", "decision"}),
                   rule="A task for the user; its body holds note lines, one fact each (`tracker act A-<n> --note`)."),
}


def kind_names(kinds=KINDS) -> str:
    """`ticket, decision or action`: the kinds named, in KINDS order."""
    names = [k for k in KINDS if k in kinds]
    return " or ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def a_kind(kind: str) -> str:
    """`a ticket`, `an action`."""
    return f"{'an' if kind[:1] in 'aeiou' else 'a'} {kind}"


def id_kind(ident: str) -> str:
    """The kind whose numbered ids have this form (`D-4`: decision); "" for an id of any other form."""
    return next((k for k, v in KINDS.items() if v.prefix and re.fullmatch(rf"{v.prefix}-\d+", ident.strip(), re.I)),
                "")


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
    def links(self) -> list[Link]:
        return parse_links(section(self.body, "Links"))[0]

    @property
    def aliases(self) -> list[str]:
        """The ids of the ticket's Issue links: another name for the ticket."""
        return [link_ident(x) for x in self.links if x.label == ISSUE] if self.kind == "ticket" else []

    @property
    def stage(self) -> str:
        """A ticket's status; once in progress, overlaid by its PR's state (PR_STAGE). A todo ticket on a branch that
        holds a PR has not started, so the PR says nothing about it. Any other record's status."""
        status = str(self.get("status", STATUSES[self.kind].values[0]))
        if self.kind != "ticket" or not STAGES.get(status, NO_STAGE).in_flight:
            return status
        return PR_STAGE.get(self.get("pr_state"), status)

    @property
    def progress(self) -> Stage:
        """What a ticket's stage says of its work; NO_STAGE for an unknown stage, and for any other record."""
        return STAGES.get(self.stage, NO_STAGE) if self.kind == "ticket" else NO_STAGE

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
        """A ticket by its stage (a merged PR closes it too); any other record by its kind's closed statuses."""
        return self.progress.closed if self.kind == "ticket" else self.stage in STATUSES[self.kind].closed

    def status_update(self, status: str) -> dict:
        """The keys a status change writes, every one of them: the status; `closed_at` now when it closes the record,
        or none when it opens it again. A ticket's first start from todo stamps `started_at`: its wait time ends and
        its cycle time starts. Its close clears `next`: its summary says what it delivered."""
        statuses, updates = STATUSES[self.kind], {"status": status}
        closes = status in statuses.closed
        if "closed_at" in KEYS[self.kind]:
            if not closes and self.get("closed_at"):
                updates["closed_at"] = None
            elif closes and self.get("status") not in statuses.closed:
                updates["closed_at"] = utc_now()
        if self.kind == "ticket":
            if self.todo and STAGES.get(status, NO_STAGE).in_flight and not self.get("started_at"):
                updates["started_at"] = utc_now()
            if closes and self.get("next"):
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


_writes = 0  # tracker files this process wrote: a Tracker rebuilds its index after any write (Tracker.index)


@contextmanager
def atomic_file(path: Path):
    """Write through an exclusively created file, then replace the destination. Keep its permissions; a new file
    is owner-only. Never open the destination for writing, including when another writer replaces it with a link."""
    global _writes
    mode = 0o600
    try:
        previous = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(previous.st_mode):
            die(f"refusing to replace {path}: not a regular file")
        mode = stat.S_IMODE(previous.st_mode)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
        tmp.chmod(mode)
        for wait in (0.05, 0.1, 0.2, 0.4, None):  # Windows refuses to replace a file while another process reads it
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if wait is None:
                    raise
                time.sleep(wait)
        _writes += 1
    finally:
        if tmp.exists():
            tmp.chmod(0o600)  # Windows cannot remove a temporary file whose saved mode is read-only
            tmp.unlink()


def atomic_write(path: Path, text: str) -> None:
    with atomic_file(path) as f:
        f.write(text.encode("utf-8"))


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
    files. Reentrant within a thread; other threads of this process wait."""
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
        self.meta, problems = parse_meta(lines)
        self.readme_problems = frontmatter_problems(text) + problems
        self._kinds: dict[str, list[Record]] = {}

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

    def action(self, ident: str) -> Record | None:
        return self.one_of("action", ident)

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

    @property
    def archived(self) -> bool:
        return self.root.parent == ARCHIVE

    def headline(self) -> str:
        """`title · status · owner`, the one line that names the work everywhere."""
        return " · ".join(str(x) for x in [self.title, self.meta.get("status"), self.meta.get("owner")] if x)

    def readme(self) -> Record:
        """The README as a record, which `set tracker`, the body edits and `migrate` change as they change a ticket."""
        return load_record(self.root / "README.md", "tracker")

    def index(self) -> dict:
        """Ids and Issue ids, then (as asked for) each ticket's dependencies and each record's waiting tickets. Built
        once per state of the files: any write through this module, or a record added to the lists, rebuilds it."""
        key = (_writes, *(len(self.records_of(kind)) for kind in KINDS))
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
        """The record of any kind with this id (a ticket first), or the ticket whose Issue link names it. Case and
        leading zeros do not count: `d-4` finds D-04."""
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
        """The tickets past todo on the branch that a ticket's open PR is based on: its PR cannot merge before theirs.
        Computed from the PR's `base`, so it ends when the PR is retargeted."""
        base = t.get("base")
        if not base or t.get("pr_state") not in OPEN_PR:
            return []
        return [o for o in self.tickets if o is not t and o.get("branch") == base and not o.todo
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
        action blocks. The text views and the viewer show it the same."""
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
        """The todo tickets that can start now, each with where it starts."""
        return [(t, s) for t in self.tickets if (s := self.start_point(t))]

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

    def pr_facts(self, t: Record) -> dict | None:
        """The PR_FACTS of a ticket's open PR; None without an open PR, or before `sync` read them."""
        if not t.get("pr") or t.get("pr_state") not in OPEN_PR:
            return None
        return self.prs.get(pr_key(self.repo_of(t), t.get("pr")))

    # -- machine state (.state.json): see STATE_RULES. state() drops what no longer applies, so a write of what it
    # read keeps the file current; raw_state() is the file as it is.
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
            ids = [i for i in ids if (r := self.lookup(i)) and r.kind == "ticket" and not r.closed]
            if ids:
                use[key] = ids

        def current(part: str, key: str, entry) -> bool:
            repo, _, branch = key.rpartition(":")
            on = [t for t in self.tickets if t.get("branch") == branch and same_repo(self.repo_of(t), repo)]
            if any(not t.closed for t in on) or any(k.endswith("@" + branch) for k in use):
                return True
            at = entry.get("at", 0) if isinstance(entry, dict) else 0
            return now - at < STATE_KEEP_DAYS * 86400 and (part == "synced" or not on)

        out = dict(state, use=use)
        for part in ("synced", "handoff"):
            out[part] = {k: v for k, v in state.get(part, {}).items() if current(part, k, v)}
        out["pr_match"] = {k: v for k, v in state.get("pr_match", {}).items() if now - v.get("at", 0) < PR_MATCH_TTL_S}
        return {k: v for k, v in out.items() if v != {}}

    def issues_to_read(self) -> list[Record]:
        """The tickets whose issue fields the model should read from their issue tracker (STATE_RULES["issues"])."""
        issues = self.raw_state().get("issues", {})
        read, asked = issues.get("read", {}), issues.get("requested", 0)
        return [t for t in self.tickets if t.aliases and (not t.closed and (
            not read.get(t.id) or read[t.id] < asked)
            or not read.get(t.id) and t.get("started_at"))]  # closed: once, for its wait time, if it has a start

    def save_state(self, state: dict) -> None:
        atomic_write(self.root / ".state.json", json.dumps(state, indent=2) + "\n")

    def data_files(self) -> list[Path]:
        """Every file that holds the tracker's facts: a change to one is a change the viewer and the watcher show."""
        return [self.root / "README.md", self.root / "log.md", self.root / ".state.json",
                *(p for kind in KINDS.values() for p in sorted((self.root / kind.folder).glob("*.md")))]


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


def all_trackers(home: Path = HOME) -> list[Tracker]:
    if not home.exists():
        return []
    return [Tracker(p) for p in sorted(home.iterdir()) if p.is_dir() and (p / "README.md").exists()]


def tracker_at(slug: str, home: Path = HOME) -> Tracker | None:
    """The tracker named `slug`, if it exists. A slug is one folder name in HOME, never a path."""
    slug = str(slug or "")
    return Tracker(home / slug) if SAFE_NAME.fullmatch(slug) and (home / slug / "README.md").exists() else None


# An archived tracker is a folder in ARCHIVE. Every list, lookup, hook and sync reads HOME alone, so it costs nothing
# there; the viewer still shows it, and `tracker unarchive` brings it back. A slug names one tracker, archived or not.
ARCHIVE = HOME / ".archive"


def archived_trackers() -> list[Tracker]:
    return all_trackers(ARCHIVE)


def archived_at(slug: str) -> Tracker | None:
    return tracker_at(slug, ARCHIVE)


RESOLUTION_LINE = re.compile(r"^(?:- )?\d{4}-\d{2}-\d{2}\b")


def resolution(d: Record) -> str:
    """A closed record's answer (a decision's): the latest dated `YYYY-MM-DD (who): answer` line of its kind's closing
    section (## Resolution), or, when a person wrote it without one, its first line (detail lines follow the answer)."""
    lines = [ln.strip() for ln in strip_comments(section(d.body, KINDS[d.kind].closing)).splitlines() if ln.strip()]
    dated = [ln for ln in lines if RESOLUTION_LINE.match(ln)]
    return (dated or lines or [""])[-1 if dated else 0].removeprefix("- ")


# ---------------------------------------------------------------- dependencies
# A ticket's `depends_on` is the one place an order or a blocker is written, always on the ticket that waits. Each
# item is a ticket (satisfied once it is merged, done or dropped), a decision or a user's action (satisfied once
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


# ---------------------------------------------------------------- whose move
# Whose turn a ticket under way waits on: this side (the user and the AI) or the people or thing named. Computed from
# the review facts `sync` reads from the PR, and the ticket's open decisions, actions and external blockers; nothing
# writes it.

# What a move reads of an open PR, in the tracker's own words: a forge's adapter (github.py) maps its own words onto
# these, so the model and the views know no forge. `sync` keeps them per open PR in .state.json `prs`, without the
# empty ones.
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
}
YOU = "you"
MOVE_RULE = (
    "A ticket under way has a move: whose turn it waits on, computed, never written. In order: merge conflicts, "
    "failing checks or requested changes not re-requested are yours; a requested review is the reviewer's; an open "
    "decision it waits on is its owner's, an open action yours; an external blocker is that blocker's; then "
    "unresolved review threads, an approval (CI's while its checks run), a draft or a PR with no review requested are "
    "yours. `next` holds your own next action: do not write a wait on a reviewer into it.")


@dataclass
class Move:
    who: str  # YOU (also for an open action), the reviewers, "CI", a decision's owner (else its id) or a blocker's id
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
    if not t.in_flight:
        return None
    is_open = t.get("pr_state") in OPEN_PR
    r = tr.pr_facts(t)
    if r is None and is_open:
        return None
    r = r or {}
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
            owner = str(d.rec.get("owner") or "")
            return Move(owner or d.ident, f"{d.ident} open" if owner else "open decision", 5)
        if d.kind == "action":  # the user's to do: this side's
            return Move(YOU, f"{d.ident} open", 5)
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
        return Move(YOU, "draft", 9)
    if is_open:
        return Move(YOU, "no review requested", 9)
    return Move(YOU, "PR closed" if t.get("pr_state") == "closed" else "no PR yet", 10)


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


def apply_prs(tr: Tracker, found: dict[str, tuple[str, dict]], facts: dict[str, dict] | None) -> list[str]:
    """Write what a forge's `sync` read, in the tracker's words: per ticket id its repo and PR (`number`; `state`: one
    of PR_EVENT; `base`; `merged_at`, UTC), each PR event a log line; and the open PRs' PR_FACTS by pr_key (None: the
    forge did not answer, so the last ones stay). Returns the change lines."""
    changes, events = [], {}
    blocked = {t.id for t in tr.tickets if tr.blockers(t)}
    for t in tr.tickets:
        if t.id not in found:
            continue
        repo, pr = found[t.id]
        upd = {"pr": str(pr["number"]), "pr_state": pr["state"], "base": pr["base"]}
        if pr["state"] == "merged":
            upd["merged_at"] = pr["merged_at"]
        diff = {k: v for k, v in upd.items() if str(t.get(k)) != str(v)}
        if diff:
            t.change(diff)
            changes.append(f"{t.id}: " + ", ".join(f"{k}={format_value(v)}" for k, v in diff.items()))
            if "pr_state" in diff:
                name = f"{repo}#{upd['pr']}" if len(tr.repos) > 1 else f"#{upd['pr']}"
                events.setdefault((name, pr["state"]), []).append(t.id)
    for (name, pr_state), ids in events.items():
        append_log(tr, f"PR {name} {PR_EVENT[pr_state]}", ids)
    changes += unblocked(tr, blocked)
    state = tr.state()
    state["last_sync"] = time.time()
    if facts is not None:
        state["prs"] = facts
    state.pop("reviews", None)  # an older version's, in GitHub's own words
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


def csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


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
