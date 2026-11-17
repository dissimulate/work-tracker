"""The contract's constants: statuses, scales, keys, value forms, text limits, record kinds, link labels and the rule
texts. `check`, `rules`, `set`, the templates and the hook texts read them from here."""

from __future__ import annotations

import calendar
import datetime as dt
import re
import time
import unicodedata
from dataclasses import dataclass

from .system import die, env_int


@dataclass(frozen=True)
class Stage:
    """What a record's status says of its work. Code asks a record (`Record.todo`, `in_flight`, `started`, `closed`,
    `dropped`); it names a status only where that one status matters (in progress against in review)."""
    todo: bool = False  # its work has yet to start
    in_flight: bool = False  # under way: started, not yet closed
    closed: bool = False
    dropped: bool = False  # closed without its work done
    derived: bool = False  # a ticket's PR gives it; no command sets it

    @property
    def started(self) -> bool:
        return self.in_flight or self.closed and not self.dropped


NO_STAGE = Stage()  # of an unknown status (`check` reports it)
# Each kind's statuses, the first a new record's, and what each says of its work. A ticket's are its stages, in the
# order the views count and sort them; its PRs overlay work under way (PR_STAGE, `Record.stage`). A new status, or a new
# kind's, is one entry here: `set`, `check`, `tracker rules` and every closed, dropped or started test follow from it.
LIFECYCLES = {
    "ticket": {"todo": Stage(todo=True), "in-progress": Stage(in_flight=True),
               "in-review": Stage(in_flight=True, derived=True),
               "done": Stage(closed=True), "dropped": Stage(closed=True, dropped=True)},
    "decision": {"open": Stage(), "closed": Stage(closed=True)},
    "action": {"open": Stage(), "done": Stage(closed=True), "dropped": Stage(closed=True, dropped=True)},
    "tracker": {"planning": Stage(), "active": Stage(), "paused": Stage(), "done": Stage(closed=True)},  # the work's
}
STAGES = LIFECYCLES["ticket"]
# The statuses a command sets, per kind; a ticket's PR adds the others
STATUSES = {kind: tuple(s for s, x in stages.items() if not x.derived) for kind, stages in LIFECYCLES.items()}
PR_STAGE = {"draft": "in-progress", "open": "in-review", "merged": "done"}  # PRs' state -> stage of work under way
PR_STATES = ("open", "draft", "merged", "closed")  # the state of a ticket's PRs together: the first any of them has
OPEN_PR = {"draft", "open"}  # the states of a PR not yet merged or closed
OPEN_STAGES = {s for s, x in STAGES.items() if not x.closed}


@dataclass(frozen=True)
class IssueStage:
    """What an issue's state means, whatever its issue tracker calls it. `order` ranks how far on the work is: the
    tracker never asks to move an issue to a stage of lower order than the one it was last read at."""
    meaning: str
    examples: str  # common names of such a state, for `tracker rules`
    order: int


# The issue stages: the agent maps each issue's state onto one by meaning, and the tracker
# asks for moves in these words (README `issue_updates`). parked and started share an order: either way is a move.
ISSUE_STAGES = {
    "unstarted": IssueStage("not started", "Scheduled, Triage, To do, Backlog", 0),
    "started": IssueStage("work under way", "In Progress, In Development", 1),
    "parked": IssueStage("started and stopped on something", "Blocked, Parked, On hold", 1),
    "review": IssueStage("in review", "Ready for Review, Code Review", 2),
    "merged": IssueStage("merged, not confirmed live", "Merged, QA", 3),
    "done": IssueStage("finished, live", "Completed, Done, Released", 4),
    "wontdo": IssueStage("closed without the work", "Won't Fix, Cancelled, Duplicate", 4),
}


def stage_of(kind: str, status) -> Stage:
    """What a status (a ticket's stage too) of the kind says of its work; NO_STAGE for an unknown one."""
    return LIFECYCLES.get(kind, {}).get(str(status), NO_STAGE)


# ---------------------------------------------------------------- scales
# A scale is a ticket key that holds a level, the same whatever the issue tracker: an issue's own value is mapped onto
# it when it is read (SCALE_RULE), so levels compare, filter and sort across issue trackers. A level is a rank, not an
# amount (two XS are not an S): nothing adds levels.
# A new scale is one SCALES entry: its ticket key, the `new` and `issue` flags, `set`, `check`, `tracker rules` and the
# UI's column follow from it.

@dataclass(frozen=True)
class Scale:
    meaning: str  # what the key says of a ticket; `tracker rules` gives it with the key
    levels: range
    names: tuple[str, ...]  # each level's name in the views, in order
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


# An issue tracker's priority words, each with its level: the priority rule names them; `migrate` reads them
# (`contract.priority_from_word`).
PRIORITY_WORDS = {"urgent": 0, "highest": 0, "critical": 0, "blocker": 0, "high": 1, "medium": 2, "normal": 2,
                  "low": 3, "lowest": 4, "trivial": 4}


def or_list(words: list[str]) -> str:
    return " or ".join([", ".join(words[:-1]), words[-1]] if len(words) > 1 else words)


SCALES = {
    "priority": Scale("how urgent the ticket is", range(5), ("P0", "P1", "P2", "P3", "P4"),
                      "0 is the most urgent, 4 the least",
                      "0 (most urgent) to 4 (least): "
                      + ", ".join(f"{or_list([w.capitalize() for w, n in PRIORITY_WORDS.items() if n == i])} {i}"
                                  for i in range(5))
                      + ", and P0-P4 their digit. Yours: from how urgent the work is"),
    "size": Scale("how much work the ticket is", range(1, 6), ("XS", "S", "M", "L", "XL"), "1 is XS, 5 XL",
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
        "started_at": ("auto", "when `set status=in-progress` first started it"),
        "closed_at": ("auto", "when its status closed it (done or dropped); a merged PR's time is merged_at"),
        "prs": ("sync", "its PRs, those from its branch and its `PR:` links, each as `<number>:<state>` (draft, open, "
                        "merged or closed); a closed one no link names is left out while another is not closed"),
        "merged_at": ("sync", "when its last PR merged, once none is open"),
        "issue_created_at": ("issue", "when the ticket's issue was created in its issue tracker"),
        "issue_stage": ("issue", "what its issue's state meant when last read; see Issue stages"),
        "issue_state": ("issue", "its issue's state when last read, by the issue tracker's own name; shown only"),
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
        "repo": ("set", "the repo's owner/name, or a list when the work spans repos; `sync` reads its PRs"),
        "status": ("set", "the work's status"),
        "owner": ("set", "who owns the work"),
        "created_at": ("auto", "when the tracker was made"),
        "labels": ("set", "this tracker's own link labels, added to the defaults"),
        "issue_updates": ("set", "on: the model moves each linked ticket's issue as its work moves, and writes its "
                                 "summary once closed and the decisions settled on it to the issue, with the issue "
                                 "tracker's tool (`tracker issue` says what is due); off or empty: the tracker never "
                                 "asks"),
        "schema": ("auto", "the tracker's format version; an older tracker migrates itself"),
    },
}
SCHEMA = 6  # the tracker format this code writes; README `schema` names a tracker's (none: older than 1)
# Keys an older format named otherwise, old: new; `migrate` renames them (`contract.renamed_keys`)
RENAMED_KEYS = {"ticket": {"updated": "updated_at", "issue_created": "issue_created_at"},
                "decision": {"opened": "created_at", "updated": "updated_at"},
                "action": {"opened": "created_at", "updated": "updated_at"},
                "tracker": {"created": "created_at"}}
ISSUE_UPDATES = ("on", "off")  # README `issue_updates`: whether the model writes the tracker's changes to issues
# `tracker issue`'s flags: the ticket key each records from the ticket's issue. Its state is a pair of flags of their
# own: `--stage` (issue_stage) and `--state` (issue_state), which needs `--stage`.
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
            "(after a commit, at a turn's end, at session start) and move it; `step` logs any "
            "they missed. With no ticket under way, commits wait unlogged for the ticket the session starts. It also "
            "counts the commits logged since the branch's tickets' `next` last changed. Uncommitted files written "
            "after it are work the tracker may not show yet",
    "handoff": "`step --pause \"<text>\"` leaves the state of unfinished work on a branch for the next "
               "session; the brief shows it first; the next `step` clears it",
    "use": "`tracker use` on a shared branch such as main: the tickets one worktree works on",
    "pr_match": "a branch's PR, asked for once in a while to find its ticket",
    "prs": "per open PR (`owner/name#n`), what `sync` last read of its reviews, checks and merge state, in the "
           "tracker's words (PR_FACTS); each sync replaces them. A ticket's move is computed from them",
    "issues": "`read`: per ticket, when `tracker issue` last recorded its issue's fields; `requested`: when the "
              "UI's Refresh asked for them again. A ticket with an Issue link is to read when open and never read "
              "or read before the request; when closed, only when never read and it has a "
              "`started_at` (its wait time needs the issue's creation time). `state_read`: per ticket, when its "
              "issue's state was last recorded; `pr_changed`: per ticket, when `sync` last saw one of its PRs change. "
              "Its state alone is to read again: with updates on, when its work would move an issue never read; "
              "after a PR change; for a session's own ticket under way, every SYNC_MIN_INTERVAL_S; when read as "
              "merged, hourly for a week after the merge, then daily, and not after 30 days. The brief and the "
              "prompt hook list the ones to read for the model, each new set once, which reads them with the issue "
              "tracker's tool; none on a tracker of a newer format than the plugin's. With README "
              "`issue_updates: on`, `history`: the answer of each decision settled when updates were first turned on "
              "(`set` records it once; with none, nothing is due and `check` warns); `listed`: per ticket, what its "
              "issue should show as `tracker issue` last listed it; `written`: per ticket, what `tracker issue <id> "
              "--written` recorded from that listing: its summary once closed, and each settled decision's "
              "answer; `moves`: per ticket, the issue stage (Issue stages) the model last moved its issue to; "
              "`held`: per ticket whose issue was last read at a stage other than its last move (a person's move, "
              "or none on this machine), the stage its work wanted then: no move is asked for while it wants the "
              "same. A read of a closed issue (done, or closed without the work) closes the ticket. A "
              "linked ticket's update is due when its issue is to move (the stage its work wants: started, parked "
              "when it waits on something not under way, review, merged, or done with no PR; once its issue was "
              "read, never to a stage of lower order than the one read, not to the model's last move, and not "
              "while held), its summary differs from the one written, or a decision that "
              "touches it has an answer other than the one written or in `history`; the brief, the prompt hook and "
              "each write that makes one due ask the model for them",
    "cleanup": f"while a branch has an unfinished ticket its entries stay. Otherwise a handoff goes once the "
               f"branch's tickets are closed, and a mark, or a handoff on a branch no ticket is on, {STATE_KEEP_DAYS} "
               f"days after it was set. `use` keeps only unfinished tickets",
}


# ---------------------------------------------------------------- values
# A key's name gives its value's form (value_form). Each form (FORMS) reads a value a command is given (`read_value`),
# holds a stored value to it (`valid_value`, for `check`) and shows it (`show_value`); a new form is a Form subclass in
# FORMS, and its keys in `value_form`.
# A time the tracker writes is UTC to the second; a `_at` key may hold a date alone, written before the tracker kept
# the time: it gives no span.

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DAY_KEYS = {"due"}
LIST_KEYS = {"depends_on", "refs", "labels", "prs"}
LIST_OR_ONE = {"repo"}  # one value, or a list when the work spans repos
CHOICES = {"issue_updates": ISSUE_UPDATES, "issue_stage": tuple(ISSUE_STAGES)}  # a key that holds one of a few words
ISSUE_STATE_MAX = 40  # an issue state's name is display text from outside: cut to this many characters when read
OUTSIDE_TEXT_CUT = {"Cc", "Cf", "Zl", "Zp"}  # Unicode categories dropped from it: controls, format, line breaks


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
            at = dt.datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", text))  # Python < 3.11 refuses a Z
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


class ChoiceForm(Form):
    """One of the key's words (CHOICES)."""

    def describe(self, key: str) -> str:
        return f"one of {'|'.join(CHOICES[key])}"

    def fits(self, key: str, value) -> bool:
        return value in CHOICES[key]

    def read(self, key: str, text: str) -> str:
        if text not in CHOICES[key]:
            raise ValueError(f"{key} '{text}' is not {self.describe(key)}")
        return text


class OutsideTextForm(Form):
    """A short name from outside the tracker (an issue's state): one line of shown text. Read, it loses the characters
    of OUTSIDE_TEXT_CUT and is cut to ISSUE_STATE_MAX, never refused: the agent records what its issue tracker says.
    Stored, anything else is refused."""

    def describe(self, key: str) -> str:
        return f"one line of at most {ISSUE_STATE_MAX} characters, without control or format characters"

    def fits(self, key: str, value) -> bool:
        return isinstance(value, str) and self.read(key, value) == value

    def read(self, key: str, text: str) -> str:
        kept = "".join(c for c in text if unicodedata.category(c) not in OUTSIDE_TEXT_CUT).strip()
        return kept if len(kept) <= ISSUE_STATE_MAX else kept[:ISSUE_STATE_MAX - 1].rstrip() + "…"


FORMS = {"time": TimeForm(), "day": DayForm(), "level": LevelForm(), "list": ListForm(), "choice": ChoiceForm(),
         "outside": OutsideTextForm(), "": Form()}
OUTSIDE_KEYS = {"issue_state"}


def value_form(key: str) -> str:
    """The form of a key's value (FORMS): "time", "day", "level" (a SCALES key), "list", "choice" (a CHOICES key),
    "outside" (an OUTSIDE_KEYS key), or "" for text."""
    return ("time" if key.endswith("_at") else "day" if key in DAY_KEYS else "level" if key in SCALES
            else "list" if key in LIST_KEYS | LIST_OR_ONE else "choice" if key in CHOICES
            else "outside" if key in OUTSIDE_KEYS else "")


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


# A record's facts as its views show them, in this order, each by its form (show_value): the UI's opened record all
# of them; `tracker context` and the brief, on one line, those in TEXT_FACTS (their first line holds the group and the
# PR). A new key, a new scale's too, shows once it is here.
FACTS = ("branch", "repo", "group", "owner", *SCALES, "due", "issue_state", "created_at", "started_at", "merged_at",
         "closed_at", "updated_at")
TEXT_FACTS = {"branch", "repo", *SCALES, "due"}


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


README_TOKEN_BUDGET = 2000
MERGED_CARRY_FORWARD_MAX = 5
STALE_DECISION_DAYS = 14
STALE_ACTION_DAYS = 7
STALE_TICKET_DAYS = 7
SYNC_MIN_INTERVAL_S = 600  # a session's hooks sync at most this often; its tickets' issue states are read as often
ISSUE_MERGED_READ_S = 3600  # an issue read as merged: read again this often for a week after the merge, then daily
ISSUE_MERGED_STOP_S = 30 * 86400  # and not at all this long after the merge
LIVE_SYNC_S = env_int("TRACKER_SYNC", 120, least=30)  # while a UI or a watch shows the work live, it syncs this often
PR_MATCH_TTL_S = 600  # how long a branch's PR, once asked, is not asked again


def dated(text: str) -> str:
    """`<today>: text`; a text that starts with a date keeps its own."""
    return text if re.match(r"\d{4}-\d{2}-\d{2}\b", text) else f"{today()}: {text}"


def today() -> str:
    return dt.date.today().isoformat()


# The longest text a command takes, in characters. Only `next` has one: the brief prints it whole for each ticket it
# shows in full, and the session that writes it can shorten it, while a view that cut it would lose the handoff's
# end. Any other text has no limit: a view that goes into every session cuts what it shows (the BRIEF_* limits in
# views). Enforced when written; `check` leaves older text alone.
TEXT_MAX = {  # kind: (characters, what it is, where the rest goes)
    "next": (400, "next", "one concrete action; the detail goes in the Plan or the PR"),
}


def fit(kind: str, text: str | None) -> None:
    """Refuse a text longer than TEXT_MAX allows."""
    most, what, rest = TEXT_MAX[kind]
    n = len(" ".join(str(text or "").split()))
    if n > most:
        die(f"the {what} is {n} characters, over {most}: {rest}")


# ---------------------------------------------------------------- template contract
# Every tracker has the same shape, and `check` enforces it, so the UI and the AI find the same things in the
# same place. The templates/ folder shows each file's shape.

README_SECTIONS = ["Context", "Goal", "Scope"]  # required, first, in this order; any sections may follow
# An optional README section: this work's standing instructions for the agent, which every brief prints
# (`views.instructions`).
README_INSTRUCTIONS = "Instructions"
README_KEYS = ["title", "repo", "status", "owner", "created_at"]  # required frontmatter; repo may be empty
SCOPE_PARTS = ["In", "Out"]  # ### subsections of ## Scope
OPEN_DECISIONS_WARN = 12  # more open than this and the list is carrying ticket-level questions

RESOLVE = '`tracker decide D-<n> --resolve "..." --by <who>`'  # a decision answered
DECIDED = '`tracker decide "<title>" --resolve "..." --by <who>`'  # a choice made on the spot, in one command
DECISION_BAR = (
    "A decision is a direction choice: it changes the Goal or Scope, a ticket's plan, order or split, a contract "
    "between tickets, a product, design or architecture rule, or it needs someone else's sign-off. Record it with "
    "`tracker decide \"<title>\" --refs <ids> --question \"...\" --option \"<choice> — <what it costs>\"` (one "
    "`--option` per choice, `--recommend \"<choice>\"` for the one you recommend) when it is raised, and resolve it "
    "with "
    f"{RESOLVE} when it is answered, including an answer given in chat (a choice made on the spot is one command: "
    f"{DECIDED}). "
    "A choice inside one ticket's build (implementation detail, review fix, naming, styling) is not a decision: "
    "it goes in that ticket's Plan, `next` or PR.")
# A decision's ## Options holds only its choices, one line each, so a UI can offer each one as an answer; what is said
# about them (a fact, a proposal, evidence) goes in ## Notes.
RECOMMENDED = "(recommended)"
OPTION_LINE = re.compile(rf"- (?:{re.escape(RECOMMENDED)} )?(.+?)(?: — (.+))?")
OPTION_RULE = (f"## Options holds only the choices, one line each: `- <choice>`, or `- <choice> — <what it costs>`; "
               f"the one recommended starts `- {RECOMMENDED} `. Facts, proposals and evidence go in ## Notes.")


@dataclass(frozen=True)
class Option:
    text: str  # the choice, as an answer that takes it says it
    cost: str = ""
    recommended: bool = False

    def line(self) -> str:
        return f"- {RECOMMENDED + ' ' if self.recommended else ''}{self.text}{f' — {self.cost}' if self.cost else ''}"


def parse_options(text: str) -> tuple[list[Option], list[str]]:
    """A decision's ## Options text (no comments) as its choices, and each line that is no choice: a dated line is a
    note."""
    options, bad = [], []
    for ln in text.splitlines():
        m = OPTION_LINE.fullmatch(ln.rstrip())
        if m and not re.match(r"\d{4}-\d{2}-\d{2}\b", m[1]):
            options.append(Option(m[1].strip(), (m[2] or "").strip(), ln.startswith(f"- {RECOMMENDED} ")))
        elif ln.strip():
            bad.append(ln.strip())
    return options, bad


def options_problems(text: str) -> list[str]:
    """What breaks OPTION_RULE in a decision's ## Options text."""
    options, bad = parse_options(text)
    return ([f"'{x[:60]}' is no choice" for x in bad]
            + (["more than one choice is recommended"] if sum(o.recommended for o in options) > 1 else []))
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
# subagent in its SubagentStart line; `tracker rules` and the issue-update request print it; the skill points to it.
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
    and its statuses LIFECYCLES[kind]; code that serves every kind reads this table, so a new kind is one entry
    here, in KEYS and in LIFECYCLES, its template and its command."""
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
                   rule=f"Carry forward ≤ {MERGED_CARRY_FORWARD_MAX} bullets once done. Ids: letters, "
                        "digits, `.`, `_`, `-`."),
    "decision": Kind("decisions", "D", "decide", ("Question", "Options", "Notes", "Resolution"), ("Question",),
                     "Resolution", frozenset({"ticket"}), f"{OPTION_RULE} ## Resolution holds one dated line per "
                                                          "answer, `YYYY-MM-DD (who): answer`; the latest holds."),
    "action": Kind("actions", "A", "act", refs=frozenset({"ticket", "decision"}),
                   rule="A task for the user; its body holds note lines, one fact each (`tracker act A-<n> --note`)."),
}


def kind_names(kinds=KINDS) -> str:
    """`ticket, decision or action`: the kinds named, in KINDS order."""
    return or_list([k for k in KINDS if k in kinds])


def a_kind(kind: str) -> str:
    """`a ticket`, `an action`."""
    return f"{'an' if kind[:1] in 'aeiou' else 'a'} {kind}"


def id_kind(ident: str) -> str:
    """The kind whose numbered ids have this form (`D-4`: decision); "" for an id of any other form."""
    return next((k for k, v in KINDS.items() if v.prefix and re.fullmatch(rf"{v.prefix}-\d+", ident.strip(), re.I)),
                "")


# Link labels every tracker accepts; a tracker adds its own in README frontmatter `labels`.
ISSUE, BLOCKER, EVIDENCE = "Issue", "Blocker", "Evidence"
DEFAULT_LABELS = ["Spec", "Design", "Doc", ISSUE, "PR", "Commit", "Code", BLOCKER, EVIDENCE, "Related"]
LABEL_RULES = {
    "Code": "a place in the code the work builds on or changes: a file, a module or a symbol, by its path or URL",
    ISSUE: "the ticket's record in an issue tracker; its id (the first word of the link title, or a bare URL's last "
           "segment) also finds the ticket: `- Issue: [PROJ-12 Login page](url)`",
    BLOCKER: "something outside the tracker that the ticket waits on; names the id listed in `depends_on`",
    EVIDENCE: "a file in the tracker's evidence/ folder (measurements, runs, scripts), linked as `evidence/<name>`; "
              "`tracker attach` adds it",
}
EVIDENCE_DIR = "evidence"


def csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]
