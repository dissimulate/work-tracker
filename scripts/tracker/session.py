"""Where a command or hook stands: the session's tracker, the branch's tickets (Match), each branch's commit mark
(`record_commits`) and handoff, what a session's brief showed, and which sessions run now (`live_sessions`)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .model import (HOME, NO_STAGE, SAFE_NAME, STAGES, WINDOWS, all_trackers, append_log, archived_at,
    atomic_write, branch_entry, die, locked, put_entry, resolution, short, state_key, tracker_at, whose_move, Record,
    Tracker)
from .git import branch_of, changed_files, cwd_repo, default_branches, git, head_of, worktree, worktree_key


def named_in(branch: str, t: Record) -> bool:
    """The branch name holds the ticket's id or one of its Issue ids, as a whole token."""
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![0-9])", branch, re.I) for i in [t.id, *t.aliases])


SUMMARY_CLOSED_NAMED = 3  # closed tickets a branch's summary names; more go as a count


@dataclass
class Match:
    tracker: Tracker
    tickets: list[Record]  # every ticket on the branch, closed ones too
    branch: str
    how: str  # "branch" (a ticket's branch key), "use" (`tracker use`), "name" (an id in the branch name), or
    # "start" (the session's tracker, on no ticket)
    chosen: list[str] = field(default_factory=list)  # `tracker start --on`: this session's open tickets

    @property
    def mine(self) -> list[Record]:
        """The tickets this session works on: its chosen ones, else every ticket on the branch."""
        return [t for t in self.tickets if t.id in self.chosen] or self.tickets

    @property
    def active(self) -> list[Record]:
        """This session's tickets in progress or in review: the work under way here."""
        return [t for t in self.mine if t.in_flight]

    @property
    def focus(self) -> list[Record]:
        """Of `mine`, those under way, else the unfinished ones: the tickets the brief shows (`summary` names the
        rest)."""
        return self.active or [t for t in self.mine if not t.closed]

    def summary(self) -> str:
        """`SS-2 in-progress; also SS-1 done`: every ticket on the branch in one line, the focus first. More than
        SUMMARY_CLOSED_NAMED closed ones as a count per stage: `also 9 done, 2 merged`."""
        rest = [t for t in self.tickets if t not in self.focus]
        closed = [t for t in rest if t.closed]
        also = [f"{t.id} {t.stage}" for t in rest if t not in closed or len(closed) <= SUMMARY_CLOSED_NAMED]
        if len(closed) > SUMMARY_CLOSED_NAMED:
            counts = Counter(t.stage for t in closed)
            also += [f"{counts[s]} {s}" for s in STAGES if counts[s]]
        line = ", ".join(f"{t.id} {t.stage}" for t in self.focus) or "no open ticket"
        return line + (f"; also {', '.join(also)}" if also else "")


# ---------------------------------------------------------------- sessions
# `tracker start <name>` ties a tracker to one agent session; the branch still names the ticket. A session with no
# tracker (and no TRACKER) gets no tracker context from the hooks, only the offer to link one (`branch_matches`), or
# at its start a link it is sure of (`hooks.sure_link`, `take_over`).
# scripts/hook.sh puts the session id in TRACKER_SESSION for the session's Bash commands; hooks get it in their input.
# Per session, `focus` keeps the tickets `tracker start --on` chose, and `seen` what the brief showed (`watch`).

SESSIONS_DIR = HOME / ".sessions"  # <session id>.json; scripts/hook.sh tests for it before it starts Python
SESSIONS_MAX = 200


def session_id() -> str:
    return (os.environ.get("TRACKER_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CODEX_THREAD_ID") or "")


def inside_home(path: str | Path) -> bool:
    return Path(path).resolve().is_relative_to(HOME.resolve())


def work_dir(cwd: str | Path | None = None) -> Path:
    """Where a command works: the cwd; from inside $TRACKER_HOME (a `cd` there to read or edit), the project directory
    the session's hooks last saw, so its branch, marks and handoff still apply."""
    cwd = Path(cwd) if cwd else Path.cwd()
    seen = load_session(session_id()).get("cwd", "") if inside_home(cwd) else ""
    return Path(seen) if seen and Path(seen).is_dir() else cwd


def session_path(sid: str) -> Path | None:
    return SESSIONS_DIR / f"{sid}.json" if sid and SAFE_NAME.fullmatch(sid) else None


def load_session(sid: str) -> dict:
    try:
        return json.loads(session_path(sid).read_text())
    except (OSError, ValueError, AttributeError):
        return {}


def save_session(sid: str, **fields) -> None:
    """Update one session's file; a None value removes that field. Keeps the newest SESSIONS_MAX sessions."""
    path = session_path(sid)
    if not path:
        return
    with locked():
        entry = {**load_session(sid), **fields}
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({k: v for k, v in entry.items() if v is not None}, indent=1))
        old = sorted(SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)[:-SESSIONS_MAX]
        for f in old:
            f.unlink(missing_ok=True)
            f.with_suffix(".edits").unlink(missing_ok=True)  # an older version's edit count


def drop_session(sid: str) -> None:
    path = session_path(sid)
    if path:
        path.unlink(missing_ok=True)


# "Not now" to the offer to link a session to a tracker, per repo and branch: no offer there for DECLINE_S.
DECLINED_FILE = HOME / ".declined.json"
DECLINE_S = 86400


def declined(cwd: str | Path) -> bool:
    try:
        at = json.loads(DECLINED_FILE.read_text()).get(state_key(cwd_repo(cwd), branch_of(cwd)), 0)
    except (OSError, ValueError, AttributeError):
        return False
    return time.time() - at < DECLINE_S


def decline(cwd: str | Path) -> None:
    with locked():
        try:
            old = json.loads(DECLINED_FILE.read_text())
        except (OSError, ValueError):
            old = {}
        now = time.time()
        keep = {k: v for k, v in old.items() if isinstance(v, (int, float)) and now - v < DECLINE_S}
        keep[state_key(cwd_repo(cwd), branch_of(cwd))] = int(now)
        atomic_write(DECLINED_FILE, json.dumps(keep, indent=1))


# `/clear` ends a session and starts a new one in the same directory. The session's end (reason "clear") leaves its
# tracker and `--on` choice here, per directory, and the new session's start (source "clear") takes them.
# scripts/hook.sh tests for this file, so it exists only while a handover waits.
CLEARED_FILE = HOME / ".cleared.json"
CLEAR_HANDOVER_S = 60


def cleared_entries(now: float) -> dict:
    try:
        old = json.loads(CLEARED_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in old.items() if isinstance(v, dict) and now - v.get("at", 0) < CLEAR_HANDOVER_S}


def save_cleared(entries: dict) -> None:
    if entries:
        atomic_write(CLEARED_FILE, json.dumps(entries, indent=1))
    else:
        CLEARED_FILE.unlink(missing_ok=True)


def hand_over(sid: str, cwd: str | Path) -> None:
    """A session that `/clear` ends: keep its tracker for the next session in this directory."""
    entry = load_session(sid)
    if not entry.get("tracker"):
        return
    with locked():
        now = time.time()
        save_cleared(cleared_entries(now) | {str(Path(cwd).resolve()): {
            "tracker": entry["tracker"], "focus": entry.get("focus"), "at": now}})


def take_over(sid: str, cwd: str | Path) -> str:
    """A session that `/clear` started: link the tracker the cleared session in this directory was on. Its slug, or
    "" when none waits."""
    with locked():
        entries = cleared_entries(time.time())
        got = entries.pop(str(Path(cwd).resolve()), None)
        save_cleared(entries)
    if not got or not tracker_at(got.get("tracker", "")):
        return ""
    save_session(sid, tracker=got["tracker"], focus=got.get("focus"))
    return got["tracker"]


def session_tracker(sid: str) -> Tracker | None:
    """The tracker `tracker start` chose for this session."""
    return tracker_at(load_session(sid).get("tracker", ""))


def session_focus(tr: Tracker, sid: str) -> list[str]:
    """The tickets `tracker start --on` chose for this session, when the session is on this tracker."""
    entry = load_session(sid)
    return entry.get("focus", []) if entry.get("tracker") == tr.slug else []


def context_tracker(sid: str | None = None) -> Tracker | None:
    """The tracker context: the tracker `tracker start` chose for this session (`sid`, default `session_id()`), or
    the one TRACKER names."""
    own = session_tracker(session_id() if sid is None else sid)
    return own or tracker_at(os.environ.get("TRACKER", ""))


def match_cwd(cwd: str | Path, sid: str | None = None, tracker: Tracker | None = None) -> Match | None:
    """The tickets on this session's branch, in the tracker context (`tracker`, else `context_tracker`); None without
    one. In order: every ticket whose `branch` is this branch (a branch holds any number); else the ones `tracker
    use` chose for this worktree and branch; else the unfinished tickets whose id or Issue id the branch name holds.
    A ticket in another repo never matches: branch names repeat across repos. With none of them, the tracker matches
    with no tickets. The open tickets `tracker start --on` chose for the session (`sid`, default `session_id()`) join
    them, and `Match.focus` picks from those alone."""
    own = tracker or context_tracker(sid)
    if not own:
        return None
    branch = branch_of(cwd)
    hits, how = branch_tickets(own, cwd, branch)
    picks = [r for r in map(own.lookup, session_focus(own, session_id() if sid is None else sid))
             if r and r.kind == "ticket" and not r.closed]
    ids = {r.id for r in hits}
    return Match(own, hits + [r for r in picks if r.id not in ids], branch, how, [r.id for r in picks])


def branch_tickets(own: Tracker, cwd: str | Path, branch: str) -> tuple[list[Record], str]:
    """The tickets on a branch, and how they were found (`Match.how`)."""
    if branch:
        slug = cwd_repo(cwd)

        def here(r: Record) -> bool:
            repo = own.repo_of(r).lower()
            return not slug or not repo or repo == slug

        hits = [r for r in own.tickets_on(branch) if here(r)]
        if hits:
            return hits, "branch"
        hits = [r for r in (own.lookup(i) for i in own.state().get("use", {}).get(worktree_key(cwd, branch), []))
                if r and r.kind == "ticket" and here(r)]
        if hits:
            return hits, "use"
        hits = [r for r in own.tickets if not r.closed and named_in(branch, r) and here(r)]
        if hits:
            return hits, "name"
    return [], "start"


def in_repos(tr: Tracker, cwd: str | Path) -> bool:
    """The cwd is a repo this tracker's work lives in (any repo when the tracker names none)."""
    return not tr.repos or cwd_repo(cwd) in (r.lower() for r in tr.repos)


def branch_matches(cwd: str | Path) -> list[Match]:
    """Every tracker with an open ticket on this branch (as `match_cwd` finds it), in a repo its work lives in: what
    `tracker start` with no name links, and what a session with no tracker is offered, or linked to, at its start."""
    found = (match_cwd(cwd, tracker=tr) for tr in all_trackers() if in_repos(tr, cwd))
    return [m for m in found if m and m.focus]


def trackers_for_repo(cwd: str | Path) -> list[Tracker]:
    slug = cwd_repo(cwd)
    return [t for t in all_trackers() if slug and slug in (r.lower() for r in t.repos)]


def find_tracker(args) -> Tracker | None:
    """The tracker named by --tracker or TRACKER, or the one the cwd, the session (`tracker start`) or the repo (the
    only tracker that lists it) points to."""
    slug = getattr(args, "tracker", None) or os.environ.get("TRACKER")
    if slug:
        return tracker_at(slug) or die(no_tracker(slug))
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if parent.parent == HOME and (parent / "README.md").exists():
            return Tracker(parent)
    own = session_tracker(session_id())
    if own:
        return own
    repo = trackers_for_repo(work_dir())
    return repo[0] if len(repo) == 1 else None


def no_tracker(slug: str) -> str:
    """The error for a slug that names no tracker; of an archived one, how to bring it back."""
    return (f"{slug} is archived: `tracker unarchive {slug}` brings it back" if archived_at(slug)
            else f"no tracker '{slug}' in {HOME}")


NO_TRACKERS = f"no trackers in {HOME}: `tracker init <slug> --title \"...\" --owner <name>` creates one"


def resolve(args) -> Tracker:
    tr = find_tracker(args)
    if tr:
        return tr
    cwd = work_dir()
    names = ", ".join(t.slug for t in all_trackers()) or die(NO_TRACKERS)
    die(f"cannot tell which tracker: this session and branch '{branch_of(cwd) or '?'}' are on no ticket. "
        f"`tracker start <name>` ties this session to a tracker, or pass --tracker <slug> (trackers: {names})")


def locate(args, ident: str) -> tuple[Tracker, Record]:
    """A record named by a command's argument, and its tracker. `slug:ID` names both. Otherwise the tracker
    `find_tracker` names is asked first (PR and branch too); then, unless --tracker or TRACKER named it,
    every tracker by ticket or Issue id: this repo's first, then all. More than one hit is an error that lists them."""
    slug, sep, rest = ident.partition(":")
    if sep:
        tr = tracker_at(slug) or die(no_tracker(slug))
        return tr, tr.find(rest)
    tr = find_tracker(args)
    rec = tr and (tr.lookup(ident) or tr.by_pr_or_branch(ident))
    if rec:
        return tr, rec
    if getattr(args, "tracker", None) or os.environ.get("TRACKER"):
        tr.find(ident)  # dies: no such record in the named tracker
    for group in (trackers_for_repo(work_dir()), all_trackers()):
        hits = [(t, r) for t in group if (r := t.lookup(ident)) and r.kind == "ticket"]
        if len(hits) > 1:
            die(f"'{ident}' is in more than one tracker: {', '.join(f'{t.slug}:{r.id}' for t, r in hits)} — "
                f"pass one of those")
        if hits:
            return hits[0]
    die(f"no ticket '{ident}' in any tracker" + (f" (nor a record in {tr.slug})" if tr else ""))


# Per repo and branch, in .state.json: the mark (the HEAD and the time the branch's commits were last logged: by the
# hooks, `step`, `synced` or `pause`), and the handoff that `pause` leaves. Only commits made after the mark by this git
# user count as new work, so a pull, a rebase or a reset does not. The hooks log commits by themselves
# (`record_commits`); the mark also counts the commits they logged since the branch's tickets' `next` last changed, so
# a `next` that the work has passed shows.

def get_mark(tr: Tracker, cwd: str | Path, branch: str) -> dict | None:
    mark = branch_entry(tr.state().get("synced", {}), cwd_repo(cwd), branch)
    if isinstance(mark, str):  # older versions kept only the HEAD
        return {"head": mark, "at": int(git(cwd, "show", "-s", "--format=%ct", mark) or 0)}
    return mark


def set_mark(tr: Tracker, cwd: str | Path, branch: str, passed: tuple[int, str] | None = (0, "")) -> str:
    """Mark the branch at HEAD. `passed`: the commits logged since its tickets' `next` last changed, and the `next`
    they were logged under (`next_key`); None keeps the mark's."""
    head = head_of(cwd)
    if not head:  # git cannot run here (as in a sandbox): keep the mark as it is
        return head
    with locked():
        state = tr.state()
        marks = state.setdefault("synced", {})
        if passed is None:
            old = branch_entry(marks, cwd_repo(cwd), branch)
            passed = (old.get("behind", 0), old.get("next", "")) if isinstance(old, dict) else (0, "")
        # `at` to the ms: a file written after it is work the tracker may not show (`lag`).
        entry = {"head": head, "at": round(time.time(), 3)}
        if passed[0]:
            entry.update(behind=passed[0], next=passed[1])
        put_entry(marks, cwd_repo(cwd), branch, entry)
        tr.save_state(state)
    return head


def next_key(m: Match) -> str:
    """The `next` of the branch's tickets as the files hold it now: when it changes, `next` was written."""
    tr = Tracker(m.tracker.root)
    pairs = sorted((r.id, str(r.get("next") or "")) for r in (tr.lookup(t.id) for t in m.tickets) if r)
    return hashlib.sha1(json.dumps(pairs).encode()).hexdigest()[:10]


def behind(m: Match, mark: dict | None) -> int:
    """The commits the hooks logged on the branch since its tickets' `next` last changed."""
    return mark.get("behind", 0) if mark and mark.get("behind") and mark.get("next") == next_key(m) else 0


def set_handoff(tr: Tracker, cwd: str | Path, branch: str, text: str | None) -> None:
    """Leave the state of unfinished work on a branch for the next session, or clear it (`text` None)."""
    with locked():
        state = tr.state()
        handoffs = state.setdefault("handoff", {})
        h = None
        if text:
            dirty = bool(git(cwd, "status", "--porcelain", "--untracked-files=no"))
            h = {"text": text.strip(), "at": int(time.time()), "head": head_of(cwd), "dirty": dirty}
        put_entry(handoffs, cwd_repo(cwd), branch, h)
        if not handoffs:
            del state["handoff"]
        tr.save_state(state)


def branch_handoff(tr: Tracker, repo: str, branch: str) -> dict | None:
    return branch_entry(tr.state().get("handoff", {}), repo, branch)


def handoff_line(h: dict, label: str = "handoff") -> str:
    where = f"at {h.get('head', '')[:9]}" + (" with uncommitted changes" if h.get("dirty") else "")
    return f"{label} ({ago(h.get('at', 0))}, {where}; the next `step` clears it): {h.get('text', '')}"


def ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    return (f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago" if s < 172800 else f"{s // 86400} d ago") \
        if ts else "never"


def new_commits(cwd: str | Path, mark: dict) -> list[tuple[str, str]]:
    """(short sha, subject) of the branch's own commits on HEAD that this git user authored after the mark, oldest
    first. A merge from the default branch, or a rebase onto it, brings in work its own tickets logged: only the
    branch's first-parent line counts, without merge commits, and off a default branch none that one holds."""
    me = git(cwd, "config", "user.email").lower()
    known = git(cwd, "rev-parse", "--verify", "--quiet", f"{mark['head']}^{{commit}}")
    rng = [f"{mark['head']}..HEAD"] if known else ["HEAD", "--max-count=200"]
    defaults = default_branches(cwd)
    held = [] if branch_of(cwd) in defaults else git(
        cwd, "for-each-ref", "--format=%(refname)",
        *(f"refs/{where}/{b}" for b in sorted(defaults) for where in ("heads", "remotes/origin"))).split()
    out = []
    for line in git(cwd, "log", "--reverse", "--first-parent", "--no-merges", "--format=%h %ae %at %s", *rng,
                    *(["--not", *held] if held else [])).splitlines():
        sha, email, at, subject = (line.split(" ", 3) + ["", "", ""])[:4]
        # The range already leaves out the commits the mark holds; the time leaves out old work a pull or a rebase
        # brought in. A commit in the same second as the mark is new.
        if (not me or email.lower() == me) and at.isdigit() and int(at) >= int(mark.get("at") or 0):
            out.append((sha, subject))
    return out


COMMITS_LOGGED_MAX = 8


def log_commits(tr: Tracker, branch: str, commits: list[tuple[str, str]], refs: list[str]) -> None:
    shown = "; ".join(f"{sha} {subject}" for sha, subject in commits[:COMMITS_LOGGED_MAX])
    more = f"; and {len(commits) - COMMITS_LOGGED_MAX} more" if len(commits) > COMMITS_LOGGED_MAX else ""
    append_log(tr, f"Commits on {branch}: {shown}{more}", refs)


def record_commits(m: Match, cwd: str | Path) -> list[tuple[str, str]]:
    """The hooks' part of the record: log the branch's commits since its mark against the tickets under way, and move
    the mark, so the model records only what git does not hold (`step`). A branch first seen gets its baseline mark;
    with no ticket under way, the commits wait unlogged for the ticket the session starts. Returns those logged."""
    if not m.branch:
        return []
    mark = get_mark(m.tracker, cwd, m.branch)
    if mark and head_of(cwd) == mark["head"]:
        return []  # most turns: no lock
    with locked():  # one hook logs a commit, even when a subagent's and the session's run at once
        mark = get_mark(m.tracker, cwd, m.branch)
        if not mark:
            set_mark(m.tracker, cwd, m.branch)
            return []
        if head_of(cwd) == mark["head"]:
            return []
        commits = new_commits(cwd, mark)
        if commits and not m.active:
            return []
        if commits:
            log_commits(m.tracker, m.branch, commits, [t.id for t in m.active])
        # With no new commit (a pull, a rebase, a reset) the mark moves and keeps its count.
        set_mark(m.tracker, cwd, m.branch, (behind(m, mark) + len(commits), next_key(m)) if commits else None)
    return commits


def mark_up_to_date(m: Match, cwd: str | Path, handoff_text: str | None = None,
                    refs: list[str] | None = None) -> list[str]:
    """`synced`, `pause` and `step`: log the commits the hooks have not against `refs` (default: `Match.focus`), move
    the mark to HEAD, and set or clear the handoff. Returns what it did."""
    tr, done = m.tracker, []
    mark = get_mark(tr, cwd, m.branch)
    commits = new_commits(cwd, mark) if mark else []
    refs = [t.id for t in m.focus] if refs is None else refs
    if commits:
        log_commits(tr, m.branch, commits, refs)
        done.append(f"logged {len(commits)} commit(s)")
    had = branch_handoff(tr, cwd_repo(cwd), m.branch)
    head = set_mark(tr, cwd, m.branch)
    if handoff_text or had:
        set_handoff(tr, cwd, m.branch, handoff_text)
    if handoff_text:
        append_log(tr, f"Paused on {m.branch}: {short(handoff_text)}", refs)
        done.append("handoff left")
    elif had:
        done.append("handoff cleared")
    return [f"{m.branch} up to date at {head[:9]}" if head else f"{m.branch} not marked: git did not run", *done]


def on_branch(args) -> tuple[Match, Path]:
    cwd = work_dir()
    m = match_cwd(cwd, tracker=find_tracker(args))
    if not m:
        die(f"this session and branch '{branch_of(cwd) or '?'}' are on no tracker: `tracker start <name>`")
    if not m.branch:
        die("not on a git branch" + (": run it from the project, not the tracker folder" if inside_home(cwd) else ""))
    return m, cwd


# ---------------------------------------------------------------- watch

def watch(tr: Tracker, tickets: list[Record]) -> dict[str, list[str]]:
    """What a session saw of its tickets, to tell it what changed since: of its own tickets the stage and a move that
    is its own (a move that goes to a reviewer asks nothing of it); of their dependencies the stage and Carry forward;
    of their decisions the status and answer."""
    seen = {}
    for t in tickets:
        move = whose_move(tr, t)
        seen[t.id] = ["own", t.stage, move.text(" — ") if move and move.mine else ""]
        for d in tr.deps(t):
            if d.kind == "ticket" and d.ident not in seen:
                cf = hashlib.sha1("\n".join(d.rec.carry_forward).encode()).hexdigest()[:10]
                seen[d.ident] = ["dep", d.rec.stage, cf]
        for d in tr.decisions_for(t):
            seen[d.id] = ["decision", d.get("status", "open"), resolution(d)]
    return seen


def changes_since(tr: Tracker, old: dict, new: dict) -> list[str]:
    out = []
    for ident, now in new.items():
        was = old.get(ident)
        if was == now:
            continue
        rec = tr.lookup(ident)
        if now[0] == "decision":
            if not was:
                out.append(f"{ident} {now[1]} now touches this work: {rec.get('title')}")
            elif now[1] == "closed":
                out.append(f"{ident} {'re-' if was[1] == 'closed' else ''}settled: {now[2]}")
            else:
                out.append(f"{ident} is {now[1]} again")
        elif not was:
            out.append(f"now waits on {ident} ({now[1]})")
        else:
            # The session's own ticket changes stage by its own doing, or sync's: only its end is news.
            if was[1] != now[1] and (now[0] != "own" or STAGES.get(now[1], NO_STAGE).closed):
                out.append(f"{ident} is now {now[1]}")
            if now[0] == "own" and len(was) > 2 and was[2] != now[2] and now[2]:  # older sessions kept no move
                out.append(f"{ident} move now: {now[2]}")
            if now[0] == "dep" and was[2:] != now[2:]:
                out.append(f"{ident} Carry forward changed (`tracker context {ident}`)")
    return out


@dataclass
class Lag:
    """The work the tracker may not show yet, from git: the commits since the branch's mark (not logged yet), the
    uncommitted files written since, and the commits logged since the tickets' `next` last changed. Edits by any
    tool, subagent or session in the worktree count."""
    commits: list[tuple[str, str]]
    files: list[str]
    since: float  # when the mark was set; 0 without one
    behind: int = 0

    def __bool__(self) -> bool:
        return bool(self.commits or self.files or self.behind)

    def text(self) -> str:
        """`2 commit(s) not logged and 3 uncommitted file(s) since the tracker last logged the branch (40 min ago); 1
        logged commit(s) since `next` last changed`."""
        parts = [f"{len(self.commits)} commit(s) not logged" if self.commits else "",
                 f"{len(self.files)} uncommitted file(s)" if self.files else ""]
        new = " and ".join(p for p in parts if p)
        return "; ".join(x for x in [
            new and f"{new} since the tracker last logged the branch ({ago(self.since)})",
            self.behind and f"{self.behind} logged commit(s) since `next` last changed"] if x)


def lag(m: Match, cwd: str | Path) -> Lag:
    mark = get_mark(m.tracker, cwd, m.branch) if m.branch else None
    if not mark:
        return Lag([], [], 0)
    return Lag(new_commits(cwd, mark), changed_files(cwd, mark.get("at") or 0), mark.get("at") or 0, behind(m, mark))


def remember(sid: str, m: Match, cwd: str | Path) -> None:
    """Keep what the session's brief showed about its tickets (the commits past `next` too, so the hooks do not ask
    again), and the directory it works in (`work_dir`)."""
    fields = {} if inside_home(cwd) else {"cwd": str(cwd)}
    if m.focus:
        fields.update(seen=watch(m.tracker, m.focus), seen_for=[t.id for t in m.focus])
    mark = get_mark(m.tracker, cwd, m.branch) if m.branch and m.active else None
    if behind(m, mark):
        fields["told_next"] = mark["next"]
    if sid and fields:
        save_session(sid, **fields)


def own_edit(sid: str, m: Match, ident: str) -> None:
    """A tracker file this session edited by hand: count its record's change, and anything it newly makes the session
    watch, as seen, so the prompt hook does not report them as another session's."""
    entry = load_session(sid)
    if entry.get("seen_for") != [t.id for t in m.focus]:
        return
    seen, now = entry.get("seen", {}), watch(m.tracker, m.focus)
    seen.update({k: v for k, v in now.items() if k == ident or k not in seen})
    save_session(sid, seen=seen)


# ---------------------------------------------------------------- live sessions
# Claude Code keeps one file per running `claude` process: <config dir>/sessions/<pid>.json, with its pid, sessionId,
# cwd, name, status ("busy" while the model works, "idle" while it waits for the user) and when the status last
# changed (statusUpdatedAt, epoch ms). Other hosts use the activity recorded by their hooks. An ended session or
# one with no hook activity for a day does not count. A session with a process file is judged by that file alone.

CLAUDE_SESSIONS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "sessions"
ACTIVITY_MAX_AGE_S = 86400


def session_activity(sid: str, status: str) -> None:
    """Keep a tracked session's status from its hooks, and when the status last changed."""
    entry = load_session(sid)
    if not entry.get("tracker"):
        return
    now = time.time()
    old = entry.get("activity", {})
    since = old.get("since", now) if old.get("status") == status else now
    save_session(sid, activity={"status": status, "since": since, "at": now})


@dataclass
class Live:
    """An agent session with a running process or recent hook activity, tied to a tracker by `tracker start`."""
    sid: str
    name: str
    status: str
    cwd: str  # where the session's hooks last saw it work, else where it started
    branch: str
    focus: list[str]  # the tickets `tracker start --on` chose
    since: float  # when the status last changed (epoch s); 0 when not known


def alive(pid: int) -> bool:
    if pid <= 0:  # 0 and -1 name process groups
        return False
    if WINDOWS:  # there os.kill(pid, 0) sends Ctrl+C (signal 0 is CTRL_C_EVENT): ask Windows instead
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: another user's
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0: only asks whether the process exists
    except PermissionError:  # another user's
        return True
    except (OSError, OverflowError):
        return False
    return True


def live_sessions(slug: str) -> list[Live]:
    """Tracked sessions on this tracker with a running process or recent hook activity, by name."""
    return live_by_tracker({slug}).get(slug, [])


def live_by_tracker(slugs: set[str] | None = None) -> dict[str, list[Live]]:
    """`live_sessions` of these trackers (None: of every tracker), by slug: one pass over the session files, however
    many trackers ask."""
    worktree.cache_clear()  # the viewer runs for hours: read each worktree's branch again
    out: dict[str, dict[str, Live]] = {}
    native = set()

    def wanted(entry: dict) -> bool:
        return bool(entry.get("tracker")) and (slugs is None or entry["tracker"] in slugs)

    def add(sid: str, entry: dict, name: str, status: str, since: float, cwd: str = "") -> None:
        cwd = entry.get("cwd") or cwd
        out.setdefault(entry["tracker"], {})[sid] = Live(
            sid, name, status, cwd, branch_of(cwd) if cwd and Path(cwd).is_dir() else "", entry.get("focus", []), since)

    for f in CLAUDE_SESSIONS.glob("*.json"):
        try:
            c = json.loads(f.read_text())
            sid, pid, since = str(c["sessionId"]), int(c["pid"]), float(c.get("statusUpdatedAt") or 0) / 1000
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        native.add(sid)
        entry = load_session(sid)
        if not wanted(entry) or not alive(pid):
            continue
        add(sid, entry, str(c.get("name") or sid[:8]), str(c.get("status") or ""), since, str(c.get("cwd") or ""))
    now = time.time()
    for f in SESSIONS_DIR.glob("*.json"):
        if f.stem in native:
            continue
        entry = load_session(f.stem)
        try:
            activity = entry.get("activity", {})
            status, since, at = activity["status"], float(activity["since"]), float(activity["at"])
        except (KeyError, ValueError, TypeError):
            continue
        if wanted(entry) and status in ("busy", "idle") and now - at < ACTIVITY_MAX_AGE_S:
            add(f.stem, entry, f.stem[:8], status, since)
    return {slug: sorted(live.values(), key=lambda s: (s.name, s.sid)) for slug, live in out.items()}
