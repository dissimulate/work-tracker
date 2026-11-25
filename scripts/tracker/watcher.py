"""`tracker watch`: a watch reports each change to a tracker, one line per ticket, agent or check: the new log lines,
the facts no log line holds (`snapshot`), the agent sessions on the tracker, and GitHub (`sync`). It runs in a terminal,
or in an agent session that the user, or a skill or instruction from them, told to watch; one per session and
tracker. A session's watch of its own tickets (`--mine`), which the turn-end hook asks for, prints only the session's
news (`session.news`)."""

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
from .schema import COORDINATOR, DEADLINE_S, LIVE_SYNC_S, QUIET_KINDS, STATE_KEEP_DAYS
from .model import last_seq, read_events, short, Tracker
from .moves import decision_move
from .session import (GRANTS, SESSIONS_DIR, WATCH_DIR, agent_tickets, claude_sessions, context_tracker, coordinated,
    grant_path, granted, is_time, items_of, load_session, move_of, news, save_session, session_path, subscribed,
    state_pid, live_sessions, session_id, ticket_facts, told, watch_running, Live, News)
from .contract import check
from .views import start_text
from .github import SKIPPED, budget, skipped, sync

POLL_S = 2  # the tracker's files and the sessions: local and cheap
SETTLE_S = 5  # after a change, wait this long for the rest of it (a step writes more than one file): one batch
IDLE_S = env_int("TRACKER_WATCH_IDLE", 600)  # an agent idle this long waits on the user
ASKING_S = DEADLINE_S["wait"]  # an agent that asks this long gets no answer soon: a prompt the user is at needs none
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
# A `mine` watch's line for a sync that fails: who can fix it, and what no longer comes; the reason last, as a long one
# is cut (LINE_MAX)
MINE_SYNC_FAILED = ("tell the user: GitHub sync fails, so no PR review, check or merge reaches this session until it "
                    "works again ({})")
# A `mine` watch's last line: no ticket of the session is open, which the watch skill says to start no watch after; or
# the session is on another tracker, where the turn-end hook asks for a watch of its own when one is needed.
MINE_DONE = "no ticket of this session is open: the watch has ended"
MINE_MOVED = ("this session is on another tracker now: this watch stops; start a watch there only when the turn-end "
              "hook asks")
REFUSED = 4  # exit code of a refusal, not a failure: this caller may not run the command, or not now (a tracker in use)

# ---------------------------------------------------------------- the grant
# A session that watches holds a grant: `tracker watch` gives it, and so does the user's /work-tracker:watch (the prompt
# hook). Ending it (`tracker watch --stop`, the skill's `stop`) stops the session's running watches at their next look.


def agent_session() -> tuple[bool, str]:
    """Whether this command runs in an agent session (`entered_via`), and its id."""
    return entered_via() == "agent", session_id()


def set_grant(sid: str, on: bool) -> None:
    """Give a session the watch, which also forgets the user's stop (`stop_watch`), or end it. Grants older than
    STATE_KEEP_DAYS go."""
    path = grant_path(sid)
    if not path:
        return
    GRANTS.mkdir(parents=True, exist_ok=True)
    if on:
        path.write_text(str(int(time.time())))
        with locked():
            if load_session(sid).get("no_watch"):
                save_session(sid, no_watch=None)
    else:
        path.unlink(missing_ok=True)
    for old in GRANTS.iterdir():
        try:
            if time.time() - old.stat().st_mtime > STATE_KEEP_DAYS * 86400:
                old.unlink()
        except OSError:
            pass


def stop_watch(sid: str) -> None:
    """The user's stop (`tracker watch --stop`, /work-tracker:watch stop): end the grant, and keep in the session's
    file, when it has one, that the user stopped it (`no_watch`), until a watch starts again: the turn-end hook then
    asks for none."""
    set_grant(sid, False)
    path = session_path(sid)
    with locked():
        if path and path.exists():
            save_session(sid, no_watch=True)


def session_name(sid: str) -> str:
    """A session's name when the host supplies it; else the start of its id."""
    return next((str(c.get("name") or sid[:8]) for c in claude_sessions() if str(c.get("sessionId")) == sid), sid[:8])


# ---------------------------------------------------------------- what changed

class Event(NamedTuple):
    kind: str  # one of EVENT_KINDS: what `--only` picks
    key: str  # what the line is about: a ticket or record id, `agent <name>`, `check`, `github`, `stuck <id>`
    urgent: bool  # it needs a human: `!`
    text: str


EVENT_KINDS = {"ticket": "a ticket's stage, move or next, or that it can start",
               "log": "a new log line, the commits logged",
               "agent": "an agent session that starts, ends its turn, waits on the user, seems stuck or ends",
               "github": "a sync that fails or works again",
               "check": "a new `check` error",
               "stuck": "a submission, a question or a wait on a human left too long"}
URGENT = "urgent"  # to `--only`: any event that needs a human, of any kind


def snapshot(tr: Tracker) -> dict:
    """The facts no log line holds: per ticket its `ticket_facts` and where it can start; the `check` errors."""
    starts = {t.id: start_text(tr, s) for t, s in tr.startable()}
    tickets = {t.id: {**ticket_facts(tr, t), "start": starts.get(t.id, "")} for t in tr.tickets}
    return {"tickets": tickets, "errors": check(tr)[0]}


def needs_you(fact: dict) -> bool:
    """A ticket's move (as `snapshot` keeps it) needs a human: it is a human's (`Move.human`), or yours and urgent."""
    return bool(fact.get("human")) or bool(fact.get("mine")) and fact.get("rank", 99) <= MOVE_URGENT


def waits(x: Live, now: float, watched: set[str]) -> bool:
    """An agent that waits on the user: it has asked for a while, or been idle for long and is not of `watched`."""
    if x.status == "busy" or not x.since:
        return False
    if x.status == "waiting":
        return now - x.since >= ASKING_S
    return x.sid not in watched and now - x.since >= IDLE_S


def watching(slug: str, live: list[Live]) -> set[str]:
    """Of the live sessions, those whose own watch of the tracker runs: one that waits on it is woken by it, so it is
    neither idle and waiting on the user (`waits`) nor stuck while busy (a host that waits inside a turn), though one
    that asks the user still is."""
    return {x.sid for x in live if watch_running(load_state(slug, x.sid))}


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
        # A move that only turned stuck is said by its item's own stuck line (`Watcher.stuck`)
        if now.get("move") and now["move"] != was.get("move") and now.get("stuck_from") != was.get("move"):
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
    once: none is new. A QUIET_KINDS one is left out. A PR's reviews are one, the latest's text with how many came
    before it: the first sync of a PR logs every review it already has."""
    out, commits, reviews = [], {}, {}  # reviews: per PR, its event's place in `out` and how many it holds
    live = coordinated(tr) if tr else False
    for e in events:
        if e["kind"] == "legacy" or e["kind"] in QUIET_KINDS:  # an agent's wait shows as the agent's line
            continue
        key = e["refs"][0] if e["refs"] else "log"
        if e["kind"] == "commits":
            commits[key] = commits.get(key, 0) + e["count"]
        elif e["kind"] == "pr.review":
            pr = e.get("pr")
            at, n = reviews.get(pr, (len(out), 0))
            reviews[pr] = (at, n + 1)
            event = Event("log", key, False, short(e["text"], 120) + (f" (+{n} earlier)" if n else ""))
            if n:
                out[at] = event
            else:
                out.append(event)
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


def agent_state(x: Live, now: float) -> str:
    """`<name> <status> <n> min`: the name is the one the host lists the session by."""
    return f"{x.name} {x.status if x.status in ('busy', 'waiting') else 'idle'}" + \
        (f" {int((now - x.since) // 60)} min" if x.since else "")


def agents_lines(tr: Tracker, now: float) -> list[str]:
    """`tracker agents`: per live agent session on the tracker, its state, tickets, branch and folder; the ids a
    program that reaches an agent knows it by (Claude Code's session name, Herdr's terminal); its transcript."""
    out = []
    for x in live_sessions(tr.slug):
        on = agent_tickets(tr, x.sid)
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
    on = {x.sid: agent_tickets(tr, x.sid) for x in live}
    opened = [d.id for d in tr.open_decisions()]
    acts = [a.id for a in tr.open_actions()]
    watched = watching(tr.slug, live)

    def agent(x: Live) -> str:
        idle = x.status not in ("busy", "waiting")
        return f"agent {x.name} watching" if idle and x.sid in watched else f"agent {agent_state(x, now)}"

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
        out.append(line(needs_you(facts[t.id]) or any(waits(x, now, watched) for x in agents),
                        " · ".join([f"{t.id} {t.stage}", *move, *map(agent, agents)])))
    out += [line(waits(x, now, watched), f"{agent(x)} on no ticket") for x in live if not on[x.sid]]
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
        if not st.get("who"):  # not a watch's state
            continue
        running, ended = watch_running(st), st.get("ended") or 0
        if running or is_time(ended) and time.time() - ended <= 86400:  # a damaged end is of no watch that ran
            started = st.get("started")
            out.append({"who": st["who"], "role": str(st.get("role") or ""), "running": running,
                        "since": started if is_time(started) else 0, "ended": 0 if running else ended})
    return sorted(out, key=lambda w: w["since"])


class SyncJob(threading.Thread):
    """One `sync` in a thread, so that a slow `gh` holds up no look; `changes` once it ends. A daemon: an exit does not
    wait for it, and each write it makes is whole or none."""

    def __init__(self, tr: Tracker):
        super().__init__(daemon=True)
        self.tr, self.changes = tr, []
        self.raised = False  # it could not run here (a busy lock, a bad file): GitHub is not what failed

    def run(self) -> None:
        budget(GH_BUDGET_S)
        try:
            self.changes = sync(self.tr, every=LIVE_SYNC_S)
        except (Exception, SystemExit) as exc:  # a busy lock, a bad file: try again next time
            self.changes, self.raised = [f"{SKIPPED}: {exc}"], True
        finally:
            budget(None)


class Watcher:
    """One tracker's watch. `claim` takes it (one watcher per tracker) and gives the lines to start with; `poll` looks
    once and gives the lines of what changed; `run` prints them until stopped, or until the first batch (`once`). `only`
    keeps the events of those EVENT_KINDS (and with URGENT, any urgent one); `hold` keeps those that need no one until
    the oldest is that many seconds old, so that they come as one batch. What it reported and holds lives in
    WATCH_DIR/<slug>.json, so a restart within CATCH_UP_S, by the same session or the terminal, misses nothing and
    repeats nothing. `mine`: a session's watch of its own tickets, which prints its news alone (`session.news`, which
    the prompt hook shares, so each thing is told once, or twice when both read it at once, never lost) and ends by
    itself when none of them is open."""

    def __init__(self, tr: Tracker, sid: str = "", who: str = "terminal", only: frozenset[str] = frozenset(),
                 hold: float = 0, role: str = "", mine: bool = False):
        self.root, self.slug, self.sid, self.who, self.only, self.hold = tr.root, tr.slug, sid, who, only, hold
        self.role = role  # COORDINATOR: this watch holds the tracker's coordinator role (`session.coordinated`)
        self.mine = mine
        # Why a `mine` look ends the watch: MINE_MOVED (the session is off this tracker) or MINE_DONE (no ticket of it
        # is open); "": it goes on
        self.ended = ""
        self.untold: News | None = None  # a `mine` look's news, told once its lines are printed (`printed`)
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
        """The state with this run's role and whether it is `mine`, and neither when it was given neither: each holds
        only while the watch that was given it runs."""
        st = {k: v for k, v in st.items() if k not in ("role", "mine")}
        return {**st, **({"role": self.role} if self.role else {}), **({"mine": True} if self.mine else {})}

    def claim(self, now: float) -> list[str]:
        """Take this session's (or the terminal's) watch of the tracker. Soon after its last run stopped: what
        changed since. Else: the state now, as the baseline. Its role is saved first, as the moves in its first lines
        read it (`session.coordinated`)."""
        with locked():  # the test and the claim are one step; the rest runs outside the lock (sync asks GitHub)
            st = load_state(self.slug, self.sid)
            pid = state_pid(st)
            if pid and pid != os.getpid() and alive(pid):
                die(f"{self.slug} is watched already, by {st.get('who')} (pid {pid}): one watch per session and "
                    "tracker", REFUSED)
            at = st.get("at")  # a damaged one is of no watch to catch up from
            # A restart of the same kind of watch: a `mine` one keeps no snapshot.
            again = type(at) in (int, float) and now - at < CATCH_UP_S and (
                st.get("mine") is True if self.mine else "snapshot" in st)
            # Since when this watch has watched: a session's that ran on until now, in the same role, keeps its start.
            # Saved before the first lines, whose moves read it for a coordinator (`session.coordinator_since`).
            kept = again and self.sid and watch_running(st) and str(st.get("role") or "") == self.role
            started = st["started"] if kept and is_time(st.get("started")) else now
            (WATCH_DIR / self.slug).mkdir(parents=True, exist_ok=True)
            set_grant(self.sid, True)  # a session's watch holds it; a watch refused above gives none
            write_json(state_path(self.slug, self.sid), self.with_role({**st, "pid": os.getpid(), "started": started}))
        if self.mine:
            # A restart (`--once`, the host tool's timeout) says only what is new: the same first line each time would
            # wake the session for nothing, and so would a sync failure it told already.
            self.st = {k: st[k] for k in ("sync_failed",) if again and k in st}  # its first look takes the signature
            lines = ([] if again else [self.first_line(now)]) + self.news_lines(self.batch(now, force=True), now)
        elif again:
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
            lines = summary(tr, self.others(), now) + lines_of(self.stuck(tr, now), now)  # each stuck item, once
        self.st.update(pid=os.getpid(), sid=self.sid, who=self.who, started=started)
        self.st = self.with_role(self.st)
        self.st.pop("ended", None)
        self.st.pop("over", None)
        self.save(now)
        return lines

    def release(self, now: float) -> None:
        self.st.update(pid=0, ended=now)
        self.save(now)

    def first_line(self, now: float) -> str:
        """A new `mine` watch's first line: the session's subscription, each ticket with its stage and move."""
        tr = self.tracker()
        tickets = [f"{t.id} {t.stage}" + (f" · move: {move.text(' — ')}" if (move := move_of(tr, t)) else "")
                   for t in subscribed(load_session(self.sid), tr)]
        return short(f"{time.strftime('%H:%M', time.localtime(now))} watching your tickets on {self.slug}: "
                     + ("; ".join(tickets) or "none open"), LINE_MAX)

    def news_lines(self, events: list[Event], now: float) -> list[str]:
        """A `mine` watch's lines: the session's news as it is, with the time: no merging by key, no `!`, no holding.
        A sync that fails is said once (`sync_told`), as nothing else would tell the session that GitHub's part of its
        news stopped."""
        def text(e: Event) -> str:
            if e.kind != "github":
                return e.text
            return "github: " + (MINE_SYNC_FAILED.format(e.text) if e.urgent else e.text)

        clock = time.strftime('%H:%M', time.localtime(now))
        return [short(f"{clock} {text(e)}", LINE_MAX) for e in events]

    def poll(self, now: float) -> list[str]:
        forget()  # git as it is now: this process runs on
        events = self.batch(now)
        lines = self.news_lines(events, now) if self.mine else self.report(events, now)
        if events or lines or self.ended:
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
        settled = force or bool(self.changed) and now - self.changed >= SETTLE_S
        if self.mine:
            return events + self.mine_batch(tr, sig, settled)
        if settled:
            self.sig, self.changed = sig, 0.0
            new = snapshot(tr)
            # An event file shorter than the watch has seen (restored from a backup) numbers its new events again.
            # What it holds at this look counts as seen, also an event written since the restore: the seq alone
            # cannot tell the two apart.
            self.st["seq"] = min(self.st["seq"], last_seq(tr))
            logged = read_events(tr, self.st["seq"])
            if logged:
                self.st["seq"] = logged[-1]["seq"]
            events += ticket_events(self.st.get("snapshot", {}), new) + log_events(logged, tr, self.role, self.sid)
            self.st["snapshot"] = new
        events += self.agents(tr, now)
        events += self.stuck(tr, now)
        named = {e.key for e in events}
        for sid, a in self.st.get("agents", {}).items():
            if named & set(a["on"]):
                self.st.setdefault("activity", {})[sid] = now
        return events

    def mine_batch(self, tr: Tracker, sig: str, settled: bool) -> list[Event]:
        """A `mine` look: the session's news (`session.news`, over its subscription) once the files settle, told once
        it is printed (`printed`). It marks the end (`ended`) when the session is on another tracker or none, or has
        no open subscribed ticket, with the news of a ticket that closed before the end."""
        if (own := context_tracker(self.sid)) is None or own.slug != self.slug:
            self.ended = MINE_MOVED
            return []  # its cursor is another tracker's now: it is not this watch's to move
        # A change waits to settle, the end too: a hand edit is written before its hook counts it as the session's
        # own (`own_edit`).
        # The wait runs from the first change seen, so an own hand edit made just before an earlier change settles can
        # still be told back once.
        if not settled:
            return []
        self.sig, self.changed = sig, 0.0
        self.ended = "" if subscribed(load_session(self.sid), tr) else MINE_DONE
        found = news(self.sid, tr)
        if found.lines:
            self.untold = found
        else:
            told(self.sid, tr, found)
        return [Event("log", "", False, x) for x in found.lines]

    def printed(self) -> None:
        """The lines so far are printed: the news they hold counts as told (`session.told`)."""
        if self.untold:
            told(self.sid, self.tracker(), self.untold)
            self.untold = None

    def github(self, now: float) -> list[Event]:
        """Start a SyncJob at most every LIVE_SYNC_S (a UI's or a hook's sync counts), and once it ends, say once when
        it failed, and once when it works again. What it changes shows as the files change."""
        if self.job and self.job.is_alive():
            return []
        # A `mine` watch says only a failure of GitHub's: the session can do nothing about a sync that could not run
        # here, and the next one tries again.
        told = self.job and not (self.mine and self.job.raised)
        out = self.sync_told(self.job.changes, now) if told else []
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
        (`waits`), or is busy so long with nothing recorded for its tickets (once per spell), but for one that waits on
        its own watch (`watching`); two on one ticket."""
        old, told, activity = self.st.get("agents", {}), self.st.setdefault("told", {}), \
            self.st.setdefault("activity", {})
        agents, out, live = {}, [], self.others()
        watched = watching(self.slug, live)
        for x in live:
            on = agent_tickets(tr, x.sid)
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
            if waits(x, now, watched):
                told[spell] = x.since
                out.append(Event("agent", key, True, f"asks for a permission or an answer{where}"
                                 if x.status == "waiting" else f"idle {mins} min{where}: waits on you"))
            elif x.status == "busy" and on and quiet >= BUSY_S and x.sid not in watched:
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

    def stuck(self, tr: Tracker, now: float) -> list[Event]:
        """A line for each open item that is stuck (`items_of`, with the clock at each look and the log read only when
        it changed), said once each time it turns stuck, and in a new watch's first lines: its key stays in the state
        while it is stuck, so one that comes back, or turns stuck anew, is said again.
        Nothing is written to the tracker. A wait whose session the agent line already shows waiting is that line's
        to say, when `only` keeps agent lines, and the watch's own session's waits are not news to it."""
        items = [i for i in items_of(tr, now) if not (i.kind == "wait" and i.session == self.sid)]
        told, agents = set(self.st.get("stuck", [])), self.st.get("agents", {})
        asks_shown = self.wanted(Event("agent", "", True, ""))
        out = []
        for i in items:
            if not i.stuck or i.key in told:
                continue
            told.add(i.key)
            if i.kind != "wait" or not asks_shown or agents.get(i.session, {}).get("status") != "waiting":
                if i.kind == "question":
                    name = i.key.split(":", 1)[1]  # its decision
                else:
                    name = i.refs[0] if i.refs else ""
                out.append(Event("stuck", f"stuck {name}".strip(), True, i.what))  # a line of its own
        self.st["stuck"] = sorted(told & {i.key for i in items if i.stuck})  # stuck again later: said again
        return out

    def run(self, once: bool) -> None:
        """A session's watch stops when the user ends it (/work-tracker:watch stop); a `mine` one also ends by itself
        (`ended`), with exit 0."""
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        lines = self.claim(time.time())
        try:
            if not once and not self.sid:  # for a person; a session's first lines say what it watches
                print(f"watching {self.slug} (Ctrl-C stops); `!` marks what needs a human", flush=True)
            while not (once and lines) and not self.ended:
                if lines:
                    print("\n".join(lines), flush=True)
                    self.printed()
                time.sleep(POLL_S)
                if self.sid and not granted(self.sid):
                    print("The watch has ended (`tracker watch --stop`): do not start it again unless told to.",
                          flush=True)
                    sys.exit(REFUSED)
                lines = self.poll(time.time())
            if lines:
                print("\n".join(lines), flush=True)  # flushed before its news counts as told
                self.printed()
            if self.ended:  # a `mine` watch with nothing left to watch here: an end, not a refusal
                print(f"{time.strftime('%H:%M')} {self.ended}", flush=True)
                self.st["over"] = True  # it does not start again (`watch_running`)
                # The grant is the session's: another watch of the session goes on with it.
                own = state_path(self.slug, self.sid)
                if not any(watch_running(read_json(f)) for f in WATCH_DIR.glob(f"*/{self.sid}.json") if f != own):
                    set_grant(self.sid, False)
                return
            if self.sid:
                print("Next: the same `tracker watch` command again, in the background.", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            if once and self.job:
                self.job.join(GH_BUDGET_S)  # else a sync cut short at each run would start again at the next
            self.release(time.time())
