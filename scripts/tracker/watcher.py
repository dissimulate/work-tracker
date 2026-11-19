"""`tracker watch`: a watch reports each change to a tracker, one line per ticket, agent or check: the new log lines,
the facts no log line holds (`snapshot`), the agent sessions on the tracker, and GitHub (`sync`). It runs in a terminal,
or in an agent session that the user, or a skill or instruction from them, told to watch; one per session and
tracker."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from .system import alive, die, entered_via, env_int
from .store import files_hash, locked, read_json, write_json
from .git import forget
from .schema import COORDINATOR, LIVE_SYNC_S, STATE_KEEP_DAYS
from .model import last_seq, read_events, short, Tracker
from .moves import decision_move
from .session import (GRANTS, SESSIONS_DIR, WATCH_DIR, claude_sessions, coordinated, grant_path, granted,
    live_sessions, match_cwd, session_id, ticket_facts, watch_running, Live)
from .contract import check
from .views import start_text
from .github import SKIPPED, budget, skipped, sync

POLL_S = 2  # the tracker's files and the sessions: local and cheap
SETTLE_S = 5  # after a change, wait this long for the rest of it (a step writes more than one file): one batch
IDLE_S = env_int("TRACKER_WATCH_IDLE", 600)  # an agent idle this long waits on the user
ASKING_S = 30  # an agent that asks this long gets no answer soon: a prompt the user is at needs no notification
BUSY_S = env_int("TRACKER_WATCH_BUSY", 2700)  # busy this long with nothing recorded: it may be stuck
CHANGE_POLL_S = 1  # `await_change`: how often it looks at the files
CHANGE_WAIT_S = 15  # when it ends with no change (ui/view.js AT_LEAST_MS): a UI that has gone leaves no process
CATCH_UP_S = 12 * 3600  # a watcher that starts again this soon reports what changed while it was stopped
# A move of yours of this rank or more urgent needs the user (MOVE_RULE: conflicts, failing checks, changes requested,
# unresolved review threads, an approval); a draft, a PR with no review asked for, a closed PR or none yet is the
# agent's to move on. A human's move (an open action, a decision a human must answer) always needs one (`needs_you`).
MOVE_URGENT = 8
LINE_MAX = 200
GH_BUDGET_S = 20  # all gh calls of one sync
REFUSED = 4  # exit code of a refusal, not a failure: this caller may not run the command, or not now (a tracker in use)

# ---------------------------------------------------------------- the grant
# A session that watches holds a grant: `tracker watch` gives it, and so does the user's /work-tracker:watch (the prompt
# hook). Ending it (`tracker watch --stop`, the skill's `stop`) stops the session's running watches at their next look.


def agent_session() -> tuple[bool, str]:
    """Whether this command runs in an agent session (`entered_via`), and its id."""
    return entered_via() == "agent", session_id()


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


def session_name(sid: str) -> str:
    """A session's name when the host supplies it; else the start of its id."""
    return next((str(c.get("name") or sid[:8]) for c in claude_sessions() if str(c.get("sessionId")) == sid), sid[:8])


# ---------------------------------------------------------------- what changed

class Event(NamedTuple):
    kind: str  # one of EVENT_KINDS: what `--only` picks
    key: str  # what the line is about: a ticket or record id, `agent <name>`, `check`, `github`
    urgent: bool  # it needs a human: `!`
    text: str


EVENT_KINDS = {"ticket": "a ticket's stage, move or next, or that it can start",
               "log": "a new log line, the commits logged",
               "agent": "an agent session that starts, ends its turn, waits on the user, seems stuck or ends",
               "github": "a sync that fails or works again",
               "check": "a new `check` error"}
URGENT = "urgent"  # to `--only`: any event that needs a human, of any kind


def snapshot(tr: Tracker) -> dict:
    """The facts no log line holds: per ticket its `ticket_facts` and where it can start; the `check` errors."""
    starts = {t.id: start_text(tr, s) for t, s in tr.startable()}
    tickets = {t.id: {**ticket_facts(tr, t), "start": starts.get(t.id, "")} for t in tr.tickets}
    return {"tickets": tickets, "errors": check(tr)[0]}


def needs_you(fact: dict) -> bool:
    """A ticket's move (as `snapshot` keeps it) needs a human: it is a human's (`Move.human`), or yours and urgent."""
    return bool(fact.get("human")) or bool(fact.get("mine")) and fact.get("rank", 99) <= MOVE_URGENT


def waits(x: Live, now: float) -> bool:
    """An agent that waits on the user: it has asked for a while, or been idle for long."""
    return x.status != "busy" and bool(x.since) and now - x.since >= (ASKING_S if x.status == "waiting" else IDLE_S)


def ticket_events(old: dict, new: dict) -> list[Event]:
    """What changed between two snapshots. A ticket new since `old` is left to its log line (`tracker new` writes
    one)."""
    out = []
    for ident, now in new.get("tickets", {}).items():
        was = old.get("tickets", {}).get(ident)
        if not was:
            continue
        if was["stage"] != now["stage"]:
            out.append(Event("ticket", ident, False, f"{was['stage']} → {now['stage']}"))
        if now.get("move") and now["move"] != was.get("move"):
            # A move that comes back from someone else (a reviewer, CI, a decision's owner) needs the user too.
            back = bool(was.get("move")) and not was.get("mine")
            out.append(Event("ticket", ident, needs_you(now) or now["mine"] and back, f"move: {now['move']}"))
        if now["next"] and now["next"] != was["next"]:
            out.append(Event("ticket", ident, False, f"next: {short(now['next'], 80)}"))
        if now["start"] and not was["start"]:
            out.append(Event("ticket", ident, False, f"can start now: {now['start']}"))
    out += [Event("check", "check", True, e) for e in new.get("errors", []) if e not in old.get("errors", [])]
    return out


def log_events(events: list[dict], tr: Tracker | None = None, role: str = "", sid: str = "") -> list[Event]:
    """One per new event of the log (`model.read_events`), keyed by the first record it names; the commits the hooks
    logged, as a count. A new action for the user needs them, and so does a question opened or changed while still
    open whose move is a human's; to the coordinator's watch (`role`), also one whose move is the coordinator's that
    another session wrote (`sid`: the watch's own), so a worker's question reaches it at once (`asks`). With no `tr`,
    no decision is looked up. A legacy event is the log from before the event file, which a migration writes all at
    once: none is new."""
    out, commits = [], {}
    live = coordinated(tr) if tr else False
    for e in events:
        if e["kind"] == "legacy":
            continue
        key = e["refs"][0] if e["refs"] else "log"
        if e["kind"] == "commits":
            commits[key] = commits.get(key, 0) + e["count"]
        else:
            out.append(Event("log", key, e["kind"] == "action.added" or asks(tr, e, live, role, sid),
                             short(e["text"], 120)))
    return out + [Event("log", key, False, f"+{n} commit{'s' * (n != 1)}") for key, n in commits.items()]


def asks(tr: Tracker | None, e: dict, live: bool, role: str, sid: str = "") -> bool:
    """A question opened or changed (the event `e`) that is still open, and whose move (`decision_move`; `live`: a
    coordinator watches) is a human's, or the coordinator's to the coordinator's own watch (`role`) when another
    session (not `sid`, the watch's own) wrote it: its own triage is no news to it."""
    d = tr.lookup(str(e.get("decision") or "")) if tr and e["kind"] in ("decision.opened", "decision.updated") else None
    if not d or d.kind != "decision" or d.closed:
        return False
    move = decision_move(tr, d, live)
    own = bool(sid) and e.get("session") == sid  # a terminal's watch has no session: nothing is its own
    return move.human or (role == COORDINATOR and move.who == COORDINATOR and not own)


def agent_tickets(tr: Tracker, x: Live) -> list[str]:
    """The tickets a session has under way, as a UI shows them."""
    m = match_cwd(x.cwd, x.sid, tr)
    return [t.id for t in m.active] if m else []


def agent_state(x: Live, now: float) -> str:
    """`<name> <status> <n> min`: the name is the one the host lists the session by."""
    return f"{x.name} {x.status if x.status in ('busy', 'waiting') else 'idle'}" + \
        (f" {int((now - x.since) // 60)} min" if x.since else "")


def agents_lines(tr: Tracker, now: float) -> list[str]:
    """`tracker agents`: per live agent session on the tracker, its state, tickets, branch and folder; the ids a
    program that reaches an agent knows it by (Claude Code's session name, Herdr's terminal); its transcript."""
    out = []
    for x in live_sessions(tr.slug):
        on = agent_tickets(tr, x)
        facts = [f"on {', '.join(on)}" if on else "", f"branch {x.branch}" if x.branch else "", x.cwd,
                 f"claude {x.claude}" if x.claude else "", f"herdr {x.terminal}" if x.terminal else "",
                 f"transcript {x.transcript}" if x.transcript else ""]
        out.append(" · ".join([agent_state(x, now), *filter(None, facts)]))
    return out or [f"no agent session on {tr.slug}"]


def lines_of(events: list[Event], now: float) -> list[str]:
    """One line per key, `!` on those that need the user, which come first."""
    keys: dict[str, tuple[bool, list[str]]] = {}
    for e in events:
        was, texts = keys.get(e.key, (False, []))
        keys[e.key] = (was or e.urgent, texts if e.text in texts else texts + [e.text])
    clock = time.strftime("%H:%M", time.localtime(now))
    return [short(f"{clock} {'! ' * urgent}{key}: {' · '.join(texts)}", LINE_MAX)
            for key, (urgent, texts) in sorted(keys.items(), key=lambda kv: not kv[1][0])]


def summary(tr: Tracker, live: list[Live], now: float) -> list[str]:
    """The first run's lines: a count line with the open decisions, the user's open actions, the tickets under way
    with their moves and agents, the agents on no ticket, and what can start."""
    clock = time.strftime("%H:%M", time.localtime(now))
    facts = snapshot(tr)["tickets"]
    flight = [t for t in tr.tickets if t.in_flight]
    on = {x.sid: agent_tickets(tr, x) for x in live}
    opened = [d.id for d in tr.open_decisions()]
    acts = [a.id for a in tr.open_actions()]

    def agent(x: Live) -> str:
        return f"agent {agent_state(x, now)}"

    def line(urgent: bool, text: str) -> str:
        return short(f"{clock} {'! ' * urgent}{text}", LINE_MAX)

    out = [line(False, f"watching {tr.slug}: {len(flight)} under way, "
                       f"{sum(needs_you(facts[t.id]) for t in flight)} your move, {len(live)} agent(s), "
                       f"{len(opened)} open decision(s)" + (f" ({', '.join(opened)})" if opened else ""))]
    if acts:
        out.append(line(True, f"{len(acts)} open action(s) for you: {', '.join(acts)} (in the UI, or `tracker "
                              f"actions`)"))
    for t in flight:
        agents = [x for x in live if t.id in on[x.sid]]
        move = [f"move: {facts[t.id]['move']}"] if facts[t.id].get("move") else []
        out.append(line(needs_you(facts[t.id]) or any(waits(x, now) for x in agents),
                        " · ".join([f"{t.id} {t.stage}", *move, *map(agent, agents)])))
    out += [line(waits(x, now), f"{agent(x)} on no ticket") for x in live if not on[x.sid]]
    ready = [t.id for t, _ in tr.startable()]
    return out + ([line(False, f"can start: {', '.join(ready)}")] if ready else [])


# ---------------------------------------------------------------- the watcher

def state_path(slug: str, sid: str = "") -> Path:
    """Where one watch of the tracker keeps its state: the session's, or the terminal's."""
    return WATCH_DIR / slug / f"{sid or 'terminal'}.json"


def load_state(slug: str, sid: str = "") -> dict:
    return read_json(state_path(slug, sid))


def drop_watches(slug: str) -> None:
    """Forget the tracker's watches: it leaves HOME."""
    for f in (WATCH_DIR / slug).glob("*.json"):
        f.unlink(missing_ok=True)


def await_change(tr: Tracker) -> None:
    """Return when the files that hold what a UI shows of the tracker change, or at CHANGE_WAIT_S: for a UI whose host
    has no folder watch, so that it starts the whole CLI only to read a change. Of the sessions, only those on this
    tracker count: one that leaves it changes the set."""
    def now() -> str:
        sessions = [f for f in SESSIONS_DIR.glob("*.json") if read_json(f).get("tracker") == tr.slug]
        watches = sorted((WATCH_DIR / tr.slug).glob("*.json"))
        return files_hash([*tr.data_files(), *sessions, *watches])  # each file's name counts

    was, end = now(), time.monotonic() + CHANGE_WAIT_S
    while time.monotonic() < end and now() == was:
        time.sleep(CHANGE_POLL_S)


def watches_of(tr: Tracker) -> list[dict]:
    """Who watches the tracker, for a UI, the oldest first: each watch's `who`, `role` (COORDINATOR, or ""), `running`
    (`watch_running`), `since` and `ended`; none that ended over a day ago."""
    out = []
    for f in (WATCH_DIR / tr.slug).glob("*.json"):
        st = read_json(f)
        ended = st.get("ended") or 0
        if not st.get("who") or not isinstance(ended, (int, float)):  # not a watch's state, or a damaged one
            continue
        running = watch_running(st)
        if running or time.time() - ended <= 86400:
            out.append({"who": st["who"], "role": str(st.get("role") or ""), "running": running,
                        "since": st.get("started", 0), "ended": 0 if running else ended})
    return sorted(out, key=lambda w: w["since"])


class SyncJob(threading.Thread):
    """One `sync` in a thread, so that a slow `gh` holds up no look; `changes` once it ends. A daemon: an exit does not
    wait for it, and each write it makes is whole or none."""

    def __init__(self, tr: Tracker):
        super().__init__(daemon=True)
        self.tr, self.changes = tr, []

    def run(self) -> None:
        budget(GH_BUDGET_S)
        try:
            self.changes = sync(self.tr, every=LIVE_SYNC_S)
        except (Exception, SystemExit) as exc:  # a busy lock, a bad file: try again next time
            self.changes = [f"{SKIPPED}: {exc}"]
        finally:
            budget(None)


class Watcher:
    """One tracker's watch. `claim` takes it (one watcher per tracker) and gives the lines to start with; `poll` looks
    once and gives the lines of what changed; `run` prints them until stopped, or until the first batch (`once`).
    `only` keeps the events of those EVENT_KINDS (and with URGENT, any urgent one); `hold` keeps those that need no one
    until the oldest is that many seconds old, so that they come as one batch. What it reported and holds lives in
    WATCH_DIR/<slug>.json, so a restart within CATCH_UP_S, by the same session or the terminal, misses nothing and
    repeats nothing."""

    def __init__(self, tr: Tracker, sid: str = "", who: str = "terminal", only: frozenset[str] = frozenset(),
                 hold: float = 0, role: str = ""):
        self.root, self.slug, self.sid, self.who, self.only, self.hold = tr.root, tr.slug, sid, who, only, hold
        self.role = role  # COORDINATOR: this watch holds the tracker's coordinator role (`session.coordinated`)
        self.st: dict = {}
        self.sig = ""  # the files' signature the snapshot was taken at
        self.changed = 0.0  # when the files changed after it; 0: they did not
        self.synced = 0.0  # when this watcher last started a sync
        self.job: SyncJob | None = None

    def tracker(self) -> Tracker:
        return Tracker(self.root)

    def save(self, now: float) -> None:
        write_json(state_path(self.slug, self.sid), {**self.st, "at": now})

    def with_role(self, st: dict) -> dict:
        """The state with this run's role, and none when it was given none: a role is held only while the watch that
        was given it runs."""
        st = {k: v for k, v in st.items() if k != "role"}
        return {**st, "role": self.role} if self.role else st

    def claim(self, now: float) -> list[str]:
        """Take this session's (or the terminal's) watch of the tracker. Soon after its last run stopped: what
        changed since. Else: the state now, as the baseline. Its role is saved first, as the moves in its first lines
        read it (`session.coordinated`)."""
        with locked():  # the test and the claim are one step; the rest runs outside the lock (sync asks GitHub)
            st = load_state(self.slug, self.sid)
            pid = int(st.get("pid") or 0)
            if pid and pid != os.getpid() and alive(pid):
                die(f"{self.slug} is watched already, by {st.get('who')} (pid {pid}): one watch per session and "
                    "tracker", REFUSED)
            (WATCH_DIR / self.slug).mkdir(parents=True, exist_ok=True)
            set_grant(self.sid, True)  # a session's watch holds it; a watch refused above gives none
            write_json(state_path(self.slug, self.sid), self.with_role({**st, "pid": os.getpid()}))
        again = now - st.get("at", 0) < CATCH_UP_S and "snapshot" in st
        if again:
            if "seq" not in st:  # saved by a version that read log.md: none of the events is new to it
                st["seq"] = last_seq(self.tracker())
            st.pop("offset", None)
            self.st = st
            lines = self.report(self.batch(now, force=True), now, flush=True)
        else:
            tr = self.tracker()
            self.sig = files_hash(tr.data_files())
            self.st = {"snapshot": snapshot(tr), "seq": last_seq(tr)}
            self.agents(tr, now)  # known from now on: the summary says what each one does
            lines = summary(tr, self.others(), now)
        self.st.update(pid=os.getpid(), sid=self.sid, who=self.who,
                       started=st.get("started", now) if again and self.sid else now)
        self.st = self.with_role(self.st)
        self.st.pop("ended", None)
        self.save(now)
        return lines

    def release(self, now: float) -> None:
        self.st.update(pid=0, ended=now)
        self.save(now)

    def poll(self, now: float) -> list[str]:
        forget()  # git as it is now: this process runs on
        events = self.batch(now)
        lines = self.report(events, now)
        if events or lines:
            self.save(now)
        return lines

    def report(self, events: list[Event], now: float, flush: bool = False) -> list[str]:
        """The lines of the events `only` keeps, with those held before: at once when one is urgent or with `flush`,
        else once the oldest is `hold` seconds old. Until then they wait in the state, so that a restart prints them."""
        held = [Event(*e) for e in self.st.pop("held", [])] + [e for e in events if self.wanted(e)]
        since = self.st.pop("held_since", now)
        if not held:
            return []
        if flush or any(e.urgent for e in held) or now - since >= self.hold:
            return lines_of(held, now)
        self.st.update(held=held, held_since=since)
        return []

    def wanted(self, e: Event) -> bool:
        return not self.only or e.kind in self.only or e.urgent and URGENT in self.only

    def batch(self, now: float, force: bool = False) -> list[Event]:
        """What changed: GitHub (asked every LIVE_SYNC_S), the files (once they settle; at once with `force`), and the
        agents. An agent is active while events name its tickets."""
        events = self.github(now)
        tr = self.tracker()  # after the sync, which may write
        sig = files_hash(tr.data_files())
        if sig != self.sig and not self.changed:
            self.changed = now
        if force or self.changed and now - self.changed >= SETTLE_S:
            self.sig, self.changed = sig, 0.0
            new = snapshot(tr)
            # An event file shorter than the watch has seen (restored from a backup) numbers its new events again.
            # ponytail: what it holds at this look counts as seen, one written since the restore too; keep a hash of
            # the last event seen to tell a restore apart if that is ever missed.
            self.st["seq"] = min(self.st["seq"], last_seq(tr))
            logged = read_events(tr, self.st["seq"])
            if logged:
                self.st["seq"] = logged[-1]["seq"]
            events += ticket_events(self.st.get("snapshot", {}), new) + log_events(logged, tr, self.role, self.sid)
            self.st["snapshot"] = new
        events += self.agents(tr, now)
        named = {e.key for e in events}
        for sid, a in self.st.get("agents", {}).items():
            if named & set(a["on"]):
                self.st.setdefault("activity", {})[sid] = now
        return events

    def github(self, now: float) -> list[Event]:
        """Start a SyncJob at most every LIVE_SYNC_S (a UI's or a hook's sync counts), and once it ends, say once when
        it failed, and once when it works again. What it changes shows as the files change."""
        if self.job and self.job.is_alive():
            return []
        out = self.sync_told(self.job.changes, now) if self.job else []
        self.job = None
        if self.tracker().repos and now - self.synced >= LIVE_SYNC_S:
            self.synced = now
            self.job = SyncJob(self.tracker())
            self.job.start()
        return out

    def sync_told(self, changes: list[str], now: float) -> list[Event]:
        failed = self.st.get("sync_failed", 0)
        if skipped(changes):
            if failed:
                return []
            self.st["sync_failed"] = now
            return [Event("github", "github", True, changes[0])]
        if failed and self.tracker().raw_state().get("last_sync", 0) > failed:
            del self.st["sync_failed"]
            return [Event("github", "github", False, "sync works again")]
        return []

    def others(self) -> list[Live]:
        """The agent sessions on the tracker but this watch's own: its own turns would wake it again and again."""
        return [x for x in live_sessions(self.slug) if x.sid != self.sid]

    def agents(self, tr: Tracker, now: float) -> list[Event]:
        """The agent sessions on the tracker: each that starts, ends its turn or ends; each that waits on the user
        (`waits`), or is busy so long with nothing recorded for its tickets (once per spell); two on one ticket."""
        old, told, activity = self.st.get("agents", {}), self.st.setdefault("told", {}), \
            self.st.setdefault("activity", {})
        agents, out = {}, []
        for x in self.others():
            on = agent_tickets(tr, x)
            agents[x.sid] = {"name": x.name, "on": on, "status": x.status, "since": x.since}
            key, where = f"agent {x.name}", f" on {', '.join(on)}" if on else ""
            was = old.get(x.sid)
            if not was:
                activity[x.sid] = now
                out.append(Event("agent", key, False, "started" + where))
            # A new idle spell, also when a whole turn fell between two looks.
            elif x.status == "idle" and x.since and (was.get("status"), was.get("since")) != ("idle", x.since):
                out.append(Event("agent", key, False, "turn ended" + where))
            if not x.since:
                continue
            spell, mins = f"{x.status}:{x.sid}", int((now - x.since) // 60)
            quiet = now - max(x.since, activity.get(x.sid, 0))
            if told.get(spell) == x.since:
                continue
            if waits(x, now):
                told[spell] = x.since
                out.append(Event("agent", key, True, f"asks for a permission or an answer{where}"
                                 if x.status == "waiting" else f"idle {mins} min{where}: waits on you"))
            elif x.status == "busy" and on and quiet >= BUSY_S:
                told[spell] = x.since
                out.append(Event("agent", key, False, f"busy {mins} min; nothing recorded for {', '.join(on)} in "
                                                      f"{int(quiet // 60)} min"))
        for sid, a in old.items():
            if sid not in agents:
                out.append(Event("agent", f"agent {a['name']}", False,
                                 "ended" + (f" on {', '.join(a['on'])}" if a["on"] else "")))
                for k in [k for k in told if k.endswith(f":{sid}")]:
                    del told[k]
                activity.pop(sid, None)
        shared: dict[str, list[str]] = {}
        for a in agents.values():
            for ident in a["on"]:
                shared.setdefault(ident, []).append(a["name"])
        shared = {i: sorted(names) for i, names in shared.items() if len(names) > 1}
        out += [Event("agent", i, False, f"{len(names)} agents on it: {', '.join(names)}")
                for i, names in shared.items() if self.st.get("shared", {}).get(i) != names]
        self.st.update(agents=agents, shared=shared)
        return out

    def run(self, once: bool) -> None:
        """A session's watch stops when the user ends it (/work-tracker:watch stop)."""
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        lines = self.claim(time.time())
        try:
            if not once and not self.sid:  # for a person; a session's first lines say what it watches
                print(f"watching {self.slug} (Ctrl-C stops); `!` marks what needs a human", flush=True)
            while not (once and lines):
                if lines:
                    print("\n".join(lines), flush=True)
                time.sleep(POLL_S)
                if self.sid and not granted(self.sid):
                    print("The watch has ended (`tracker watch --stop`): do not start it again unless told to.",
                          flush=True)
                    sys.exit(REFUSED)
                lines = self.poll(time.time())
            print("\n".join(lines))
            if self.sid:
                print("Next: the same `tracker watch` command again, in the background.", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            if once and self.job:
                self.job.join(GH_BUDGET_S)  # else a sync cut short at each run would start again at the next
            self.release(time.time())
