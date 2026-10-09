"""`tracker watch`: one watcher per tracker reports each change that may need the user, one line per ticket, agent or
check: the new log lines, the facts no log line holds (moves, `next`, stages, tickets that can start, `check` errors),
the agent sessions on the tracker, and GitHub (`sync`). Only the user starts it: in a terminal, or in an agent session
they gave the watch with its skill command (a grant the prompt hook writes). Also archiving and deleting a tracker,
which refuse while an agent session or a watch is on it."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from .model import (ARCHIVE, HOME, IN_FLIGHT, SAFE_NAME, STATE_KEEP_DAYS, atomic_write, die, files_hash, locked,
    short, to_trash, whose_move, Tracker)
from .session import (CLAUDE_SESSIONS, SESSIONS_DIR, alive, drop_session, live_sessions, load_session, match_cwd,
    session_id, Live)
from .contract import check
from .views import LOG_LINE, start_text
from .github import budget, sync

WATCH_DIR = HOME / ".watch"  # <slug>.json: a tracker's watcher and what it reported; grants/<session id>
GRANTS = WATCH_DIR / "grants"
POLL_S = 2  # the tracker's files and the sessions: local and cheap
SETTLE_S = 5  # after a change, wait this long for the rest of it (a step writes more than one file): one batch
SYNC_S = int(os.environ.get("TRACKER_WATCH_SYNC", "120"))  # GitHub; a viewer's or a hook's sync counts too
IDLE_S = int(os.environ.get("TRACKER_WATCH_IDLE", "600"))  # an agent idle this long waits on the user
BUSY_S = int(os.environ.get("TRACKER_WATCH_BUSY", "2700"))  # busy this long with nothing recorded: it may be stuck
CATCH_UP_S = 12 * 3600  # a watcher that starts again this soon reports what changed while it was stopped
REARM_S = 600  # a session's watch runs `--once` again and again: between two runs it still watches
# A move of this rank or more urgent needs the user (MOVE_RULE: conflicts, failing checks, changes requested, open
# review threads, ready to merge); a draft, a PR not yet open or with no review asked for is the agent's to move on.
MOVE_URGENT = 8
LINE_MAX = 200
GH_BUDGET_S = 20  # all gh calls of one sync
REFUSED = 4  # exit code: not the user's watch, or another watcher has the tracker

# ---------------------------------------------------------------- the user's grant
# Only a prompt the user types fires the UserPromptSubmit hook, so the grant it writes is the user's: no command the
# model runs gives one. In an agent session `tracker watch` runs only with it; in a terminal it needs none.


def agent_session() -> tuple[bool, str]:
    """Whether this command runs in an agent session, and its id."""
    sid = session_id()
    return bool(sid) or os.environ.get("CLAUDECODE") == "1", sid


def grant_path(sid: str) -> Path | None:
    return GRANTS / sid if sid and SAFE_NAME.fullmatch(sid) else None


def granted(sid: str) -> bool:
    path = grant_path(sid)
    return bool(path and path.exists())


def set_grant(sid: str, on: bool) -> None:
    """Give a session the watch, or end it. Grants older than STATE_KEEP_DAYS go."""
    path = grant_path(sid)
    if not path:
        return
    GRANTS.mkdir(parents=True, exist_ok=True)
    if on:
        path.write_text(str(int(time.time())))
    else:
        path.unlink(missing_ok=True)
    for old in GRANTS.iterdir():
        try:
            if time.time() - old.stat().st_mtime > STATE_KEEP_DAYS * 86400:
                old.unlink()
        except OSError:
            pass


def watching() -> bool:
    """This command runs in a session the user gave the watch: it watches only, and writes nothing to trackers."""
    inside, sid = agent_session()
    return inside and granted(sid)


def session_name(sid: str) -> str:
    """A session's name when the host supplies it; else the start of its id."""
    for f in CLAUDE_SESSIONS.glob("*.json"):
        try:
            c = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(c, dict) and str(c.get("sessionId")) == sid:
            return str(c.get("name") or sid[:8])
    return sid[:8]


# ---------------------------------------------------------------- what changed
# An event is (key, needs the user, text); a batch prints one line per key, those that need the user first.

Event = tuple[str, bool, str]


def snapshot(tr: Tracker) -> dict:
    """The facts no log line holds: per ticket its stage, move, `next` and where it can start; the `check` errors."""
    starts = {t.id: start_text(tr, s) for t, s in tr.startable()}
    tickets = {}
    for t in tr.tickets:
        move = whose_move(tr, t)
        tickets[t.id] = {"stage": t.stage, "next": str(t.get("next") or ""), "start": starts.get(t.id, ""),
                         **({"move": move.text(), "rank": move.rank, "mine": move.mine} if move else {})}
    return {"tickets": tickets, "errors": check(tr)[0]}


def needs_you(fact: dict) -> bool:
    """A ticket's move (as `snapshot` keeps it) is the user's to make."""
    return bool(fact.get("mine")) and fact.get("rank", 99) <= MOVE_URGENT


def waits(x: Live, now: float) -> bool:
    """An agent idle so long that it waits on the user."""
    return x.status != "busy" and bool(x.since) and now - x.since >= IDLE_S


def ticket_events(old: dict, new: dict) -> list[Event]:
    """What changed between two snapshots. A ticket new since `old` is left to its log line (`new` writes one)."""
    out = []
    for ident, now in new.get("tickets", {}).items():
        was = old.get("tickets", {}).get(ident)
        if not was:
            continue
        if was["stage"] != now["stage"]:
            out.append((ident, False, f"{was['stage']} → {now['stage']}"))
        if now.get("move") and now["move"] != was.get("move"):
            # A move that comes back from someone else (a reviewer, CI, a decision's owner) needs the user too.
            back = bool(was.get("move")) and not was.get("mine")
            out.append((ident, needs_you(now) or now["mine"] and back, f"move: {now['move']}"))
        if now["next"] and now["next"] != was["next"]:
            out.append((ident, False, f"next: {short(now['next'], 80)}"))
        if now["start"] and not was["start"]:
            out.append((ident, False, f"can start now: {now['start']}"))
    out += [("check", True, e) for e in new.get("errors", []) if e not in old.get("errors", [])]
    return out


COMMITS = re.compile(r"Commits on \S+: (.*)")


def log_events(lines: list[str]) -> list[Event]:
    """One event per new log line, keyed by the first id it names; the commits the hooks logged, as a count."""
    out, commits = [], {}
    for line in lines:
        m = LOG_LINE.match(line)
        if not m:
            continue
        refs = (m[2] or "").split()
        key, msg = (refs[0] if refs else "log"), line[m.end():].strip()
        if c := COMMITS.match(msg):
            items = c[1].split("; ")
            more = re.fullmatch(r"and (\d+) more", items[-1])
            commits[key] = commits.get(key, 0) + (len(items) - 1 + int(more[1]) if more else len(items))
        else:
            out.append((key, False, short(msg, 120)))
    return out + [(key, False, f"+{n} commit{'s' * (n != 1)}") for key, n in commits.items()]


def read_log(tr: Tracker, offset: int) -> tuple[list[str], int]:
    """The whole lines added to log.md since `offset` (bytes), and the new offset. A log shorter than the offset was
    rewritten (`migrate`, a hand edit): nothing counts as new, and the offset starts again at its end."""
    try:
        with open(tr.root / "log.md", "rb") as f:
            size = f.seek(0, 2)
            if size < offset:
                return [], size
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], 0
    done = data.rfind(b"\n") + 1  # a line still being written waits for the next look
    return data[:done].decode(errors="replace").splitlines(), offset + done


def agent_tickets(tr: Tracker, x: Live) -> list[str]:
    """The tickets a session has under way, as the viewer shows them."""
    m = match_cwd(x.cwd, x.sid, tr)
    return [t.id for t in m.active] if m else []


def lines_of(events: list[Event], now: float) -> list[str]:
    """One line per key, `!` on those that need the user, which come first."""
    keys: dict[str, tuple[bool, list[str]]] = {}
    for key, urgent, text in events:
        was, texts = keys.get(key, (False, []))
        keys[key] = (was or urgent, texts if text in texts else texts + [text])
    clock = time.strftime("%H:%M", time.localtime(now))
    return [short(f"{clock} {'! ' * urgent}{key}: {' · '.join(texts)}", LINE_MAX)
            for key, (urgent, texts) in sorted(keys.items(), key=lambda kv: not kv[1][0])]


def summary(tr: Tracker, live: list[Live], now: float) -> list[str]:
    """The first run's lines: the tickets under way with their moves and agents, the agents on none, the open
    decisions and what can start."""
    clock = time.strftime("%H:%M", time.localtime(now))
    facts = snapshot(tr)["tickets"]
    flight = [t for t in tr.tickets if t.stage in IN_FLIGHT]
    on = {x.sid: agent_tickets(tr, x) for x in live}
    opened = [d.id for d in tr.open_decisions()]

    def agent(x: Live) -> str:
        return f"agent {x.name} {'busy' if x.status == 'busy' else 'idle'}" + \
            (f" {int((now - x.since) // 60)} min" if x.since else "")

    def line(urgent: bool, text: str) -> str:
        return short(f"{clock} {'! ' * urgent}{text}", LINE_MAX)

    out = [line(False, f"watching {tr.slug}: {len(flight)} under way, "
                       f"{sum(needs_you(facts[t.id]) for t in flight)} your move, {len(live)} agent(s), "
                       f"{len(opened)} open decision(s)" + (f" ({', '.join(opened)})" if opened else ""))]
    for t in flight:
        agents = [x for x in live if t.id in on[x.sid]]
        move = [f"move: {facts[t.id]['move']}"] if facts[t.id].get("move") else []
        out.append(line(needs_you(facts[t.id]) or any(waits(x, now) for x in agents),
                        " · ".join([f"{t.id} {t.stage}", *move, *map(agent, agents)])))
    out += [line(waits(x, now), f"{agent(x)} on no ticket") for x in live if not on[x.sid]]
    ready = [t.id for t, _ in tr.startable()]
    return out + ([line(False, f"can start: {', '.join(ready)}")] if ready else [])


# ---------------------------------------------------------------- the watcher

def state_path(slug: str) -> Path:
    return WATCH_DIR / f"{slug}.json"


def load_state(slug: str) -> dict:
    try:
        st = json.loads(state_path(slug).read_text())
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def watcher_of(tr: Tracker) -> dict:
    """Who watches the tracker, for the viewer: `who`, `running`, `since` and `ended`; {} when no watch ran in a day.
    A session's watch runs `--once` again and again, so while it holds the grant it still runs for REARM_S after each
    run."""
    st = load_state(tr.slug)
    if not st.get("who"):
        return {}
    ended, sid = st.get("ended", 0), str(st.get("sid") or "")
    running = alive(int(st.get("pid") or 0)) or granted(sid) and time.time() - ended < REARM_S
    if not running and time.time() - ended > 86400:
        return {}
    return {"who": st["who"], "running": running, "since": st.get("started", 0), "ended": 0 if running else ended}


class Watcher:
    """One tracker's watch. `claim` takes it (one watcher per tracker) and gives the lines to start with; `poll` looks
    once and gives the lines of what changed; `run` prints them until stopped, or until the first batch (`once`).
    What it reported lives in WATCH_DIR/<slug>.json, so a restart misses nothing and repeats nothing."""

    def __init__(self, tr: Tracker, sid: str = "", who: str = "terminal"):
        self.root, self.slug, self.sid, self.who = tr.root, tr.slug, sid, who
        self.settle = SETTLE_S
        self.st: dict = {}
        self.sig = ""  # the files' signature the snapshot was taken at
        self.changed = 0.0  # when the files changed after it; 0: they did not
        self.synced = 0.0  # when this watcher last asked GitHub

    def tracker(self) -> Tracker:
        return Tracker(self.root)

    def save(self, now: float) -> None:
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(state_path(self.slug), json.dumps({**self.st, "at": now}, indent=1))

    def claim(self, now: float) -> list[str]:
        """Take the tracker's watch. Soon after this watcher stopped (same session, or the terminal): what changed
        since. Else: the state now, as the baseline."""
        with locked():  # the test and the claim are one step; the rest runs outside the lock (sync asks GitHub)
            st = load_state(self.slug)
            pid = int(st.get("pid") or 0)
            if pid and pid != os.getpid() and alive(pid):
                die(f"{self.slug} is watched already, by {st.get('who')} (pid {pid}): one watcher per tracker",
                    REFUSED)
            WATCH_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(state_path(self.slug), json.dumps({**st, "pid": os.getpid()}, indent=1))
        again = st.get("sid", "") == self.sid and now - st.get("at", 0) < CATCH_UP_S and "snapshot" in st
        if again:
            self.st = st
            lines = self.batch(now, force=True)
        else:
            tr = self.tracker()
            self.sig = files_hash(tr.data_files())
            self.st = {"snapshot": snapshot(tr), "offset": read_log(tr, 0)[1]}
            self.agents(tr, now)  # known from now on: the summary says what each one does
            lines = summary(tr, live_sessions(self.slug), now)
        self.st.update(pid=os.getpid(), sid=self.sid, who=self.who,
                       started=st.get("started", now) if again and self.sid else now)
        self.st.pop("ended", None)
        self.save(now)
        return lines

    def release(self, now: float) -> None:
        self.st.update(pid=0, ended=now)
        self.save(now)

    def poll(self, now: float) -> list[str]:
        lines = self.batch(now)
        if lines:
            self.save(now)
        return lines

    def batch(self, now: float, force: bool = False) -> list[str]:
        """What changed: GitHub (asked every SYNC_S), the files (once they settle; at once with `force`), and the
        agents. An agent is active while events name its tickets."""
        events = self.github(now)
        tr = self.tracker()  # after the sync, which may write
        sig = files_hash(tr.data_files())
        if sig != self.sig and not self.changed:
            self.changed = now
        if force or self.changed and now - self.changed >= self.settle:
            self.sig, self.changed = sig, 0.0
            new = snapshot(tr)
            logged, self.st["offset"] = read_log(tr, self.st.get("offset", 0))
            events += ticket_events(self.st.get("snapshot", {}), new) + log_events(logged)
            self.st["snapshot"] = new
        events += self.agents(tr, now)
        named = {key for key, _, _ in events}
        for sid, a in self.st.get("agents", {}).items():
            if named & set(a["on"]):
                self.st.setdefault("activity", {})[sid] = now
        return lines_of(events, now)

    def github(self, now: float) -> list[Event]:
        """Sync PR state at most every SYNC_S (a viewer's or a hook's sync counts), and say once when it fails, and
        once when it works again. What it changes shows as the files change."""
        tr = self.tracker()
        if not tr.repos or now - self.synced < SYNC_S:
            return []
        self.synced = now
        budget(GH_BUDGET_S)
        try:
            changes = sync(tr, force=False, min_interval=SYNC_S)
        except (Exception, SystemExit) as exc:  # a busy lock, a bad file: try again next time
            changes = [f"sync skipped: {exc}"]
        finally:
            budget(None)
        failed = self.st.get("sync_failed", 0)
        if changes and changes[0].startswith("sync skipped"):
            if failed:
                return []
            self.st["sync_failed"] = now
            return [("github", True, changes[0])]
        if failed and self.tracker().raw_state().get("last_sync", 0) > failed:
            del self.st["sync_failed"]
            return [("github", False, "sync works again")]
        return []

    def agents(self, tr: Tracker, now: float) -> list[Event]:
        """The agent sessions on the tracker: each that starts or ends; each idle so long that it waits on the user,
        or busy so long with nothing recorded for its tickets (once per spell); two on one ticket."""
        old, told, activity = self.st.get("agents", {}), self.st.setdefault("told", {}), \
            self.st.setdefault("activity", {})
        agents, out = {}, []
        for x in live_sessions(self.slug):
            on = agent_tickets(tr, x)
            agents[x.sid] = {"name": x.name, "on": on}
            key, where = f"agent {x.name}", f" on {', '.join(on)}" if on else ""
            if x.sid not in old:
                activity[x.sid] = now
                out.append((key, False, "started" + where))
            if not x.since:
                continue
            spell, mins = f"{x.status}:{x.sid}", int((now - x.since) // 60)
            quiet = now - max(x.since, activity.get(x.sid, 0))
            if told.get(spell) == x.since:
                continue
            if waits(x, now):
                told[spell] = x.since
                out.append((key, True, f"idle {mins} min{where}: waits on you"))
            elif x.status == "busy" and on and quiet >= BUSY_S:
                told[spell] = x.since
                out.append((key, False, f"busy {mins} min; nothing recorded for {', '.join(on)} in "
                                        f"{int(quiet // 60)} min"))
        for sid, a in old.items():
            if sid not in agents:
                out.append((f"agent {a['name']}", False, "ended" + (f" on {', '.join(a['on'])}" if a["on"] else "")))
                for k in [k for k in told if k.endswith(f":{sid}")]:
                    del told[k]
                activity.pop(sid, None)
        shared: dict[str, list[str]] = {}
        for a in agents.values():
            for ident in a["on"]:
                shared.setdefault(ident, []).append(a["name"])
        shared = {i: sorted(names) for i, names in shared.items() if len(names) > 1}
        out += [(i, False, f"{len(names)} agents on it: {', '.join(names)}") for i, names in shared.items()
                if self.st.get("shared", {}).get(i) != names]
        self.st.update(agents=agents, shared=shared)
        return out

    def run(self, once: bool) -> None:
        """Print each batch until stopped (Ctrl-C), or the first one (`once`). A session's watch stops when the user
        ends it (/work-tracker:watch stop)."""
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        lines = self.claim(time.time())
        try:
            if not once:
                print(f"watching {self.slug} (Ctrl-C stops); `!` marks what needs you", flush=True)
            while not (once and lines):
                if lines:
                    print("\n".join(lines), flush=True)
                time.sleep(POLL_S)
                if self.sid and not granted(self.sid):
                    print("The user ended the watch (/work-tracker:watch stop): do not start it again.", flush=True)
                    sys.exit(REFUSED)
                lines = self.poll(time.time())
            print("\n".join(lines))
            if self.sid:
                print(f"Next: `tracker watch {self.slug} --once` again, in the background.", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            self.release(time.time())


# ---------------------------------------------------------------- archive and delete
# A tracker leaves HOME only while no agent session or watch is on it: it would go from under them mid-work. Anyone
# may archive one, as `tracker unarchive` brings it back; only the user deletes one, to the system's trash.


class Refused(Exception):
    """A tracker that cannot be archived, brought back or deleted now; the message says why and what to do."""


def in_use(tr: Tracker, own_sid: str = "", live: dict[str, list[Live]] | None = None) -> str:
    """What keeps the tracker in HOME: the agent sessions on it, but `own_sid` (the session that asks lets go of it),
    and the watch; empty when none, and for an archived tracker, which nothing can be on. `live`: `live_by_tracker`,
    read once for many trackers."""
    if tr.archived:
        return ""
    sessions = live_sessions(tr.slug) if live is None else live.get(tr.slug, [])
    names = [x.name for x in sessions if x.sid != own_sid]
    w = watcher_of(tr)
    using = ([f"agent session{'s' * (len(names) > 1)} {', '.join(names)}"] if names else []) + \
        ([f"the watch by {w['who']}"] if w.get("running") else [])
    return " and ".join(using)


def refuse_in_use(tr: Tracker, verb: str, own_sid: str = "") -> None:
    using = in_use(tr, own_sid)
    if using:
        raise Refused(f"{tr.slug} is in use by {using}: end them (or run `tracker start --clear` in each session), "
                      f"then {verb} it")


def forget(slug: str) -> None:
    """Drop the tracker's watch state and the sessions' ties to it: nothing points at it once it leaves HOME."""
    state_path(slug).unlink(missing_ok=True)
    for f in SESSIONS_DIR.glob("*.json"):
        if load_session(f.stem).get("tracker") == slug:
            drop_session(f.stem)


def move_tracker(tr: Tracker, dest: Path) -> None:
    """Move the folder; a linked folder's link moves, made absolute, so it still names its folder."""
    if dest.exists() or dest.is_symlink():
        raise Refused(f"{dest} exists: delete or rename it, then try again")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if tr.root.is_symlink():
        dest.symlink_to(tr.root.resolve(), target_is_directory=True)
        tr.root.unlink()
    else:
        tr.root.rename(dest)


def archive_tracker(tr: Tracker, own_sid: str = "") -> str:
    """Move the tracker to ARCHIVE: out of every list, lookup and hook. Raises Refused while it is in use."""
    with locked():
        if tr.archived:
            raise Refused(f"{tr.slug} is archived already")
        refuse_in_use(tr, "archive", own_sid)
        move_tracker(tr, ARCHIVE / tr.slug)
        forget(tr.slug)
    return (f"archived {tr.slug}: the viewer still shows it, and `tracker unarchive {tr.slug}` brings it back")


def unarchive_tracker(tr: Tracker) -> str:
    with locked():
        if not tr.archived:
            raise Refused(f"{tr.slug} is not archived")
        move_tracker(tr, HOME / tr.slug)
    return f"brought {tr.slug} back from the archive"


def delete_tracker(tr: Tracker) -> str:
    """Delete the tracker, archived or not: its folder to the system's trash (of a linked folder, only the link goes).
    Raises Refused while it is in use, and OSError when the trash refuses the folder, which then stays. Says what it
    did."""
    with locked():
        refuse_in_use(tr, "delete")
        if tr.root.is_symlink():
            target = tr.root.resolve()
            tr.root.unlink()
            done = f"deleted the link {tr.root}; the folder it named stays at {target}"
        else:
            to_trash(tr.root)
            done = f"moved {tr.root} to the trash"
        forget(tr.slug)
    return done
