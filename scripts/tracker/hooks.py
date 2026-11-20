"""Agent hooks: scripts/hook.sh runs `run_hook` with the handlers its shell filters let through, and the hook JSON on
stdin."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from .system import env_int, spawn, warn
from .store import locked, record_cli, OtherFormat
from .schema import CITE, DECIDED, ISOLATION_RULE, RESOLVE, SCHEMA, STEP_MESSAGE, SYNC_MIN_INTERVAL_S
from .model import append_event, pr_key, pr_name, read_events, set_branch, short, Record, Tracker
from .moves import PR_PAGE
from .git import cwd_repo, default_branches
from .session import (behind, branch_matches, context_tracker, declined, get_mark, hand_over, in_repos, keep_folder,
    keep_word, lag, load_session, match_cwd, move_of, news, own_edit, record_commits, relink_session, remember,
    ensure_subscription, save_session, session_activity, shown_tickets, start_subscription, subscribed, take_over,
    told_about, watch_running,
    work_dir, Lag, Match)
from .contract import other_format, prune_backups, refuse_other_format, upgrade_trackers
from .views import brief, issue_request, issue_update_request, updates_told
from .github import PR_COMMAND, budget, match_pr, sync
from .watcher import load_state, set_grant, stop_watch

PR_TRIGGER = re.compile(rf"{PR_COMMAND.pattern}|\bgit\s+push\b")  # the forge's PR commands, or a push

# ---------------------------------------------------------------- hooks
# A hook adds context only when it changes what the model does next, and says each thing once.


def record_version() -> None:
    """Record how a UI outside any session starts the CLI (`record_cli`). On a new plugin version, or a change to the
    mod's files, also bring the Herdr mod the user installed to it (`update_mod`) and remove old backups
    (`prune_backups`). `ui` loads only then."""
    if record_cli():
        from .ui import update_mod
        update_mod()
        prune_backups()


def hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}


def where(data: dict) -> tuple[str, str]:
    """The hook's cwd and session id."""
    return data.get("cwd") or os.getcwd(), str(data.get("session_id") or "")


def emit_context(event: str, text: str, notice: str = "") -> None:
    """`text` goes to the model; `notice`, if any, is shown to the user in the transcript."""
    if event in ("SessionStart", "SubagentStart"):
        text = "\n".join(x for x in (command_context(), text) if x)
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    print(json.dumps(out | ({"systemMessage": notice} if notice else {})))


def command_context() -> str:
    """A host with no shell env file gets the CLI path and the session id in context, as a command prefix."""
    root, sid = os.environ.get("PLUGIN_ROOT"), os.environ.get("TRACKER_SESSION")
    if not root or not sid or os.environ.get("CLAUDE_ENV_FILE"):
        return ""
    command = f"env TRACKER_SESSION={shlex.quote(sid)} {shlex.quote(str(Path(root) / 'bin/tracker'))}"
    return f"[work-tracker] For every tracker command in this session, use `{command}` as the command prefix."


def offer(found: list[Match]) -> tuple[str, str]:
    """For a session with no tracker on a branch with open tickets: the model's request to ask the user whether to
    link it, and the line the user sees at once (a hook cannot start a model turn in an interactive session, so the
    model asks at the first message). Only the user's answer links; the hook writes nothing. A sure match
    (`sure_link`) links without the question. A tracker in another format is told (`contract.other_format`), not
    offered: its `start` would refuse."""
    why = {m.tracker.slug: other_format(m.tracker.root) for m in found}
    refused = "".join(f"\n[work-tracker] {x}." for x in why.values() if x)
    found = [m for m in found if not why[m.tracker.slug]]
    if not found:
        return refused.lstrip(), refused.lstrip()
    names = "; ".join(f"{m.tracker.slug} ({m.tracker.title}): {m.summary()}" for m in found[:3])
    options = [f'"Link {m.tracker.slug}" (run `tracker start {m.tracker.slug}`, which prints the brief)'
               for m in found[:3]]
    ask = (f"[work-tracker] This session is on no tracker. Branch `{found[0].branch}` has open tickets in {names}.\n"
           "In your first reply, before other work, use the host's question tool to ask whether to link this session "
           f"to it. Options: {', '.join(options)}, and \"Not now\" (run `tracker start --decline`: no link or offer "
           "on this branch for a day). Skip the question when the user's message already answers it.") + refused
    return ask, (f"[work-tracker] Branch `{found[0].branch}` has open tickets in {names}. The agent asks at your first "
                 "message whether to link this session.") + refused


def sure_link(found: list[Match], cwd: str | Path, sid: str) -> Match | None:
    """The match a session with no tracker links at its start without asking the user: one tracker only, and a
    ticket's `branch` names this branch, which is not a default branch (where many works share a branch, the user
    chooses), in this repo by name (a ticket with no repo matches any repo, and branch names repeat across repos).
    None for a tracker in another format (`contract.other_format`): the session stays on no tracker, and `offer`
    tells it."""
    if len(found) != 1 or found[0].how != "branch" or not sid:
        return None
    m, slug = found[0], cwd_repo(cwd)
    if not slug or any(m.tracker.repo_of(t).lower() != slug for t in m.tickets):
        return None
    return m if m.branch not in default_branches(cwd) and not other_format(m.tracker.root) else None


AUTO_LINKED = ("Linked at session start: this branch is the branch of {ids}. If the user says this session is not "
               "that work, run `tracker start --decline` (it unlinks, and no link or offer on this branch for a day).")


def hook_session_start(data: dict) -> None:
    cwd, sid = where(data)
    source = data.get("source")
    kept = take_over(sid, cwd) if source == "clear" and sid else ""  # /clear keeps the session's tracker
    folder, cwd = cwd, work_dir(cwd)  # a `cd` to the tracker: the checkout the session works in
    m = match_cwd(cwd, sid)
    note, notice = "", f"[work-tracker] This session stays on {kept} after /clear." if kept else ""
    if not m:
        # CLAUDE_CODE_SESSION_ATTENDED: 1 when a person uses the session, 0 when no one can answer (`claude -p`).
        unattended = os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0"
        asked = source == "compact" or unattended or declined(cwd)  # asked once; or no one can answer
        found = [] if asked else branch_matches(cwd)
        sure = sure_link(found, cwd, sid)
        if not sure:
            if found:
                emit_context("SessionStart", *offer(found))
            elif command_context():
                emit_context("SessionStart", "")
            return
        relink_session(sid, tracker=sure.tracker.slug)  # a wait it left open ends at its next event
        m = match_cwd(cwd, sid)
        start_subscription(sid, m)
        note = AUTO_LINKED.format(ids=", ".join(t.id for t in sure.focus))
        notice = (f"[work-tracker] Linked this session to {sure.tracker.slug}: branch `{sure.branch}` holds "
                  f"{sure.summary()}. To unlink, tell the agent this is not that work.")
    keep_folder(sid, m.tracker, folder)  # the session's file only, so before the format guard
    ensure_subscription(sid, m)
    refuse_other_format(m.tracker.root)  # the tracker /clear kept (take_over): its brief asks for writes it refuses
    record_version()
    m, found = match_pr(m, cwd)
    record_commits(m, cwd)  # made since the last session, as in the terminal
    remember(sid, m, cwd)
    note = "\n\n".join(x for x in (note, found) if x)
    text = brief(m, cwd, note, compact=source == "compact", tickets=subscribed(load_session(sid), m.tracker))
    # The brief lists the updates, so no prompt repeats them, and carries the protocol, so a session told its tracker
    # took no write (`tell_refusal`) needs none from `tracker start` now.
    fields = {"told_updates": updates_told(m.tracker), "told_refusal": None}
    # After the brief and `remember`: what the sync changes is news for the next message, not lost in the brief.
    sync_later(m.tracker, load_session(sid), fields)
    if sid:
        save_session(sid, **fields)
    emit_context("SessionStart", text, notice)


def hook_session_end(data: dict) -> None:
    """A session that `/clear` ends leaves its tracker to the next one (`hand_over`); log the last commits. The
    handover first: the host may cut a SessionEnd hook short."""
    if data.get("reason") == "clear":
        cwd, sid = where(data)
        hand_over(sid, cwd)
    hook_stop(data, ask=False)


def adopt_branch(m: Match, cwd: str | Path) -> None:
    """A branch that holds one ticket's id in its name (made when the user asked): record it on the ticket
    (`set_branch`)."""
    if m.how != "name" or len(m.tickets) != 1 or m.tickets[0].get("branch") or m.branch in default_branches(cwd):
        return  # the ticket's branch read first: it costs no git run and no lock
    with locked():
        tr = Tracker(m.tracker.root)
        t = tr.lookup(m.tickets[0].id)
        if t and not t.get("branch"):
            set_branch(tr, t, m.branch)


def next_request(m: Match, cwd: str | Path, sid: str) -> str:
    """Once per `next` a session is on: the commits the hooks logged since the tickets under way last changed their
    `next`, and the command that records the step they may have ended. Empty while `next` is newer than every commit."""
    mark = get_mark(m.tracker, cwd, m.branch) if m.branch and m.active else None
    n = behind(m, mark)
    if not n or load_session(sid).get("told_next") == mark["next"]:
        return ""
    save_session(sid, told_next=mark["next"])
    nexts = "; ".join(f"{t.id} next: {short(t.get('next'), 80)}" if t.get("next") else f"{t.id} has no next"
                      for t in m.active)
    return (f"[work-tracker] {n} commit(s) on {m.branch} since `next` last changed, logged by the hooks ({nexts}). If "
            f"a step ended or `next` no longer holds: `tracker step <id> --next \"...\"`, with {STEP_MESSAGE}, and "
            "`--carry \"...\"` for each fact a later ticket must know.")


WATCH_ASK = ("[work-tracker] {waits}. Nothing wakes this session when that changes. Start a watch of your tickets, "
             "then end the turn: `tracker watch --mine`, run as the work-tracker:watch skill says for `mine`. If the "
             "user told you not to watch, end the turn.")


def session_match(data: dict) -> tuple[Match | None, Path, str]:
    """The checkout a hook of the session runs in (its folder; inside $TRACKER_HOME, the one it last worked in:
    `work_dir`), its match there, and the session's id. A session on a tracker keeps the folder (`keep_folder`: a turn
    a watch line started brings no message) and, if it has none yet, gets its subscription (`ensure_subscription`)."""
    folder, sid = where(data)
    cwd = work_dir(folder)
    m = match_cwd(cwd, sid)
    if m:
        keep_folder(sid, m.tracker, folder)
        ensure_subscription(sid, m)
    return m, cwd, sid


def hook_stop(data: dict, ask: bool = True) -> bool:
    """The end of a turn or of the session: log the commits no other hook saw (made in the terminal, or by a command
    the post-bash filter did not let through); the model records only what git does not hold, when `next_request`
    asks. Then, at a turn's end (`ask`), the reminder to watch (`watch_ask`): the one time a hook holds a session
    back, once per wait. Claude Code's answer that holds the turn is `decision` "block", with the text as `reason`.
    Whether it held the turn, which then goes on. The commits are this checkout's; the waits are the session's
    subscription's (`subscribed`)."""
    m, cwd, sid = session_match(data)
    if not m:
        return False
    if m.branch:
        adopt_branch(m, cwd)
        record_commits(m, cwd)
    if not ask:
        return False
    text = watch_ask(data, m.tracker, subscribed(load_session(sid), m.tracker), sid)
    if text:
        print(json.dumps({"decision": "block", "reason": text}))
    return bool(text)


def watch_ask(data: dict, tr: Tracker, tickets: list[Record], sid: str) -> str:
    """The reminder to a session whose tickets (`tickets`: its subscription) wait on someone else (their move is not
    its own), with no watch of its own running on the tracker: WATCH_ASK, once per wait (`told_watch`: each ticket and
    whose move it waits on, as last seen), else "". Only where someone can act on it: the host says the hook does not
    hold the session already (`stop_hook_active` false: a host that sends no such key cannot say so, and is never
    asked), not a subagent's stop, a session a person attends, a tracker in this code's format (no `refusal_told`
    covers Stop), and not after the user stopped the session's watch (`no_watch`). A stop where it is not free to ask
    uses up nothing."""
    if data.get("stop_hook_active") is not False or data.get("agent_id") \
            or os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0" or other_format(tr.root):
        return ""
    with locked():
        entry = load_session(sid)
        if entry.get("no_watch"):
            return ""
        waits = {f"{t.id}:{move.who}": f"{t.id} waits on {move.text()}" for t in tickets
                 if (move := move_of(tr, t)) and not move.mine}
        told, keys = entry.get("told_watch") or [], sorted(waits)
        # Saved whether asked or not: a wait that ends and comes back is new again, and one that began while a watch
        # ran is not asked for once that watch stops.
        if keys != told:
            save_session(sid, told_watch=keys or None)
    if not set(keys) - set(told) or watch_running(load_state(tr.slug, sid)):
        return ""
    return WATCH_ASK.format(waits="; ".join(waits[k] for k in keys))


COMMIT_TRIGGER = re.compile(r"\bgit\b[^|;&\n]*\bcommit\b")


def hook_post_bash(data: dict) -> None:
    """After a `gh pr` command that changes a PR (PR_COMMAND) or a push: record the branch on its ticket
    (`adopt_branch`) and sync PR state; after `gh pr create`, also log the PR it opened (`log_opened`). After a commit:
    log it, and when it is the first since `next` last changed, ask whether a step ended, while the work is fresh. A
    subagent's commit (its tool calls fire these hooks too) is logged, but the subagent is asked nothing: the session
    records the step, asked at its next message."""
    subagent = bool(data.get("agent_id"))
    command = (data.get("tool_input") or {}).get("command", "")
    pr, commit = PR_TRIGGER.search(command), COMMIT_TRIGGER.search(command)
    if not (pr or commit):
        return
    cwd, sid = where(data)
    m = match_cwd(cwd, sid)
    if not m:
        return
    out = []
    if pr:
        adopt_branch(m, cwd)
        if PR_CREATE.search(command):  # before the sync, which can fail or run out of time: nothing sees it again
            log_opened(m.tracker, data.get("tool_response"))
        changes = sync(m.tracker, every=0)
        if changes and not subagent:
            out.append("[work-tracker] Synced PR state: " + "; ".join(changes))
    if commit:
        record_commits(m, cwd)
        if not subagent and (ask := next_request(m, cwd, sid)):
            out.append(ask)
    if out:
        emit_context("PostToolUse", "\n".join(out))


PR_CREATE = re.compile(r"\bgh\s+pr\s+(create|new)\b")  # `new` is gh's own alias of `create`


def log_opened(tr: Tracker, response) -> None:
    """Log the PR a `gh pr create` opened (`pr.opened`), once per PR: gh prints its address as the output's last
    line. Claude Code gives a Bash command's output as `stdout`. A command that failed reaches no `post-bash` hook,
    and one whose output ends in anything else (`--dry-run` prints the body last) logs nothing. A tracker the format
    guard refuses gets no event."""
    out = response.get("stdout") if isinstance(response, dict) else None
    last = (out.strip().splitlines() or [""])[-1].strip() if isinstance(out, str) else ""
    found = PR_PAGE.fullmatch(last)
    if not found:
        return
    repo, number = found.groups()
    key = pr_key(repo, number)
    try:
        with locked():
            tr = Tracker(tr.root)
            if not any(e["kind"] == "pr.opened" and e.get("pr") == key for e in read_events(tr)):
                append_event(tr, "pr.opened", f"PR {pr_name(tr, repo, number)} opened by a session", [], pr=key)
    except OtherFormat:
        pass


def hook_edit(data: dict) -> None:
    """A hand edit of a tracker file (hook.sh starts no other): count its change as this session's (`own_edit`)."""
    cwd, sid = where(data)
    m = match_cwd(work_dir(cwd), sid)
    tool = data.get("tool_input") or {}
    paths = [tool["file_path"]] if tool.get("file_path") else re.findall(
        r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", tool.get("command", ""), re.M)
    for path in paths:
        edited = (Path(cwd) / path).resolve()
        if m and edited.is_relative_to(m.tracker.root.resolve()):
            own_edit(sid, m.tracker, edited.stem)


NUDGE_EVERY = env_int("TRACKER_NUDGE_EVERY", 5, least=1)


def state_line(m: Match, work: Lag) -> str:
    """One line: the session's work under way, what the tracker may not show yet, and what to record."""
    lagging = f"; {work.text()}" if work else ""
    if m.active:
        where = "; ".join(f"{t.id} {t.stage}" + (f" (next: {short(t.get('next'), 60)})" if t.get("next") else "")
                          for t in m.active)
        return (f"[work-tracker] {where}{lagging}. Record each step (`step`) and direction decisions (`decide`) as "
                "they happen.")
    start = [t.id for t in m.focus] or [t.id for t, _ in m.tracker.startable()]
    return (f"[work-tracker] No ticket in progress on {m.branch or 'this branch'}{lagging}. If this is tracked work, "
            "start its ticket: `tracker set <id> status=in-progress`" + (f" ({', '.join(start[:6])})" if start else "")
            + ".")


def sync_later(tr: Tracker, entry: dict, fields: dict) -> None:
    """Pull GitHub in the background once the last sync is older than SYNC_MIN_INTERVAL_S (at most that often per
    session, by `fields`' sync_asked): a later message reports what it changed (`news`), such as a review
    that makes a move yours. The hook does not wait for it."""
    now = time.time()
    last = max(tr.state().get("last_sync", 0), entry.get("sync_asked", 0))
    if not tr.repos or now - last < SYNC_MIN_INTERVAL_S:
        return
    fields["sync_asked"] = int(now)
    try:
        spawn("--tracker", tr.slug, "sync")
    except OSError:  # the hook's own lines still go out; a message after SYNC_MIN_INTERVAL_S tries again
        pass


WATCH_PROMPT = re.compile(r"[/$]work-tracker:watch\b(.*)", re.S)  # not a bare /watch: another plugin's


def watch_request(data: dict) -> bool:
    """The user typed /work-tracker:watch: give this session the watch, which its skill then starts, or end it (`stop`)
    at once, also while the model is busy: a running watch stops at its next look."""
    m = WATCH_PROMPT.fullmatch(str(data.get("prompt") or "").strip())
    sid = where(data)[1]
    if not m or not sid:
        return False
    if m[1].split() == ["stop"]:
        stop_watch(sid)
    else:
        set_grant(sid, True)
    return True


def hook_prompt(data: dict) -> None:
    """On tracked work, each user message is kept for `decide --cite` (`keep_word`), and gets what other sessions or
    GitHub changed since its last message (or its brief), `next_request` when commits (a subagent's too) passed `next`,
    the issue fields a Refresh asked for and an issue update newly due (updates_told), each once. With a ticket under
    way, the first message and every NUDGE_EVERY-th after it get the state line, so it stays a reminder, not noise. With
    none, only work since the branch's mark brings it: at once, once per mark, then on those messages while the work
    stays. With a ticket of the session open, a stale GitHub sync starts in the background (`sync_later`). run_hook
    handles a watch request first. The state line is this checkout's; the news is the session's subscription's
    (`subscribed`)."""
    m, cwd, sid = session_match(data)
    if not m:
        return
    keep_word(sid, str(data.get("prompt") or ""))
    record_version()
    entry = load_session(sid)
    n = entry.get("prompts", 0)
    tickets = subscribed(entry, m.tracker)
    ids = [t.id for t in told_about(entry, m.tracker)]
    told = news(sid, m.tracker)
    fields = {"prompts": n + 1}
    lines = []
    # A session whose full watch of this tracker runs has them from that watch; a watch of its own tickets shares this
    # baseline, so what it printed is not here, and a watch of another tracker shows none of them.
    watched = load_state(m.tracker.slug, sid)
    if told and not (watch_running(watched) and not watched.get("mine")):
        lines.append(f"[work-tracker] Changed on {', '.join(ids)} since the brief or the user's previous message (by "
                     "another session or a PR sync): " + "; ".join(told) + ".")
    ask = next_request(m, cwd, sid)
    if ask:
        lines.append(ask)  # it holds what the state line would say
    asked = m.tracker.issue_reads()[1]
    if asked and asked != entry.get("told_issues"):  # a UI's Refresh: passed on once
        fields["told_issues"] = asked
        lines += [x for x in [issue_request(m.tracker, shown_tickets(m, tickets))] if x]
    updates = updates_told(m.tracker)
    if set(updates) - set(entry.get("told_updates") or []):  # a new issue update due: passed on once
        lines.append(issue_update_request(m.tracker, shown_tickets(m, tickets)))
    fields["told_updates"] = updates
    idle = not m.active and bool(m.branch) and in_repos(m.tracker, cwd)
    mark = get_mark(m.tracker, cwd, m.branch) if idle else None
    told = bool(mark) and entry.get("idle_told") == (mark.get("at") or 0)  # said for this mark: no git until the next
    work = lag(m, cwd) if not ask and (n % NUDGE_EVERY == 0 or idle and not told) else None
    new = work is not None and bool(work.commits or work.files)
    # With no ticket under way, the brief named what can start: the line comes back only for work since the mark.
    due = n % NUDGE_EVERY == 0 and (m.active or new)
    if work is not None and (due or new and entry.get("idle_told") != work.since):
        lines.append(state_line(m, work))
        if idle and new:
            fields["idle_told"] = work.since
    if tickets:  # a session with no open ticket learns of GitHub at its next start
        sync_later(m.tracker, entry, fields)
    if sid:
        save_session(sid, **fields)
    if lines:
        emit_context("UserPromptSubmit", "\n".join(lines))


# An option that only says go or stop. A question whose options are all such asks for approval, not for a direction.
ASSENT = re.compile(r"(yes|no|ok|okay|approved?|reject|cancel|proceed|continue|go ahead|confirm|allow|deny|skip|"
                    r"not now|later|stop|abort|wait|don'?t)\b", re.I)


def approval_only(tool_input: dict) -> bool:
    questions = tool_input.get("questions") or []
    labels = [str(o.get("label", "") if isinstance(o, dict) else o).strip()
              for q in questions for o in q.get("options") or []]
    return bool(labels) and all(ASSENT.match(x) for x in labels)


def hook_answered(data: dict) -> None:
    """The user answered a question the agent asked: an answer that settles direction is a decision made, and its
    words can be cited (`keep_word`). A yes or no to go ahead settles none."""
    if approval_only(data.get("tool_input") or {}) or data.get("agent_id"):
        return
    cwd, sid = where(data)
    kept = keep_word(sid, answer_text(data))
    if match_cwd(cwd, sid):
        ask = (f"[work-tracker] If the answer settles a direction decision, record it: {RESOLVE}, or {DECIDED} for "
               "one made on the spot.")
        # An answer not kept (another host's question tool) cannot be cited: `--cite` would be refused.
        emit_context("PostToolUse", ask if kept else ask.replace(f" {CITE}", ""))


def answer_text(data: dict) -> str:
    """The human's answers in Claude Code's AskUserQuestion result, joined by "; ", never the questions: its
    `answers` maps each question to its answer's text. "" for another shape, or another host's tool, whose result
    this code has not seen."""
    response = data.get("tool_response")
    answers = response.get("answers") if isinstance(response, dict) else None
    if data.get("tool_name") != "AskUserQuestion" or not isinstance(answers, dict):
        return ""
    texts = list(answers.values())
    return "; ".join(texts) if all(isinstance(x, str) for x in texts) else ""


def hook_subagent_start(data: dict) -> None:
    """A subagent of a session on a tracker reads it, but leaves the writing to the session, which sees the whole turn
    and records it once."""
    cwd, sid = where(data)
    m = match_cwd(cwd, sid)
    if m:
        ids = ", ".join(t.id for t in shown_tickets(m, subscribed(load_session(sid), m.tracker)))
        emit_context("SubagentStart", f"[work-tracker] The session that started you works on tracker {m.tracker.slug}"
                     + (f", ticket {ids}" if ids else "") + ". Read it when you need to (`tracker index` for the "
                     "whole work; `tracker context <id>` and `tracker show <id> --section <name>` for one ticket or "
                     "decision, by its id, not the tracker's name), but do not write it: run no `tracker` command that "
                     "changes it, and edit no tracker file. The hooks log your commits; put what else the session "
                     "should record (a finished step, a fact a later ticket needs, a decision, a blocker) in your "
                     f"final answer. {ISOLATION_RULE}")


HOOKS = {"session-start": hook_session_start, "stop": hook_stop, "session-end": hook_session_end,
         "post-bash": hook_post_bash, "edit": hook_edit,
         "prompt": hook_prompt, "answered": hook_answered, "subagent-start": hook_subagent_start,
         "waiting": lambda data: None, "resumed": lambda data: None, "stop-failure": lambda data: None}


# The handlers whose event can tell the session's model, and that event as emit_context names it: not SubagentStart,
# whose text reaches the subagent (`tell_refusal`).
CONTEXT_EVENTS = {"session-start": "SessionStart", "prompt": "UserPromptSubmit", "post-bash": "PostToolUse",
                  "answered": "PostToolUse"}


# What the model does after `contract.other_format`'s sentence until the user acts (`refusal_request`): for a newer
# format the user reloads; for an older one that could not migrate, no reload helps, and the user fixes what `check`
# reports. A reload or a fix may start no new session, so no SessionStart brings the brief: `tracker start` prints it.
RELOAD_REQUEST = ("Record nothing in it and run only `tracker` commands that read; wait for the user to reload the "
                  "plugin or restart the session, then run `tracker start`, which prints the brief.")
REPAIR_REQUEST = ("Record nothing in it and run only `tracker` commands that read; wait for the user to fix what "
                  "stops its migration, then run `tracker start`, which prints the brief.")


def refusal_request(root: Path) -> str:
    """The model's request for the tracker at `root`, which `other_format` refuses."""
    return RELOAD_REQUEST if Tracker(root).schema > SCHEMA else REPAIR_REQUEST


def tell_refusal(data: dict, event: str, root: Path) -> None:
    """The session's tracker, at `root`, takes no write from this code: tell the user why (`contract.other_format`),
    and the model why and what to do until the user acts (`refusal_request`), once per context, at the first
    handler that can tell the model. A session start is a new context, after compaction too: it is told again, with
    the command prefix (`emit_context`). Not a subagent's: the session acts on it, not the subagent."""
    sid, why = where(data)[1], other_format(root)
    notice = f"[work-tracker] {why}."
    text = f"{notice} {refusal_request(root)}"
    told = event != "session-start" and load_session(sid).get("told_refusal") == text
    if event not in CONTEXT_EVENTS or data.get("agent_id") or told:
        warn(f"hook {event}: {why}")
        return
    save_session(sid, told_refusal=text)
    emit_context(CONTEXT_EVENTS[event], text, notice)


def refusal_told(data: dict, event: str) -> bool:
    """Whether the session's tracker is in another format, on an event that can tell the model: then `tell_refusal`
    says so, and no handler runs, as each would ask the model for a write that is refused. A refusal in a handler that
    cannot tell the model (Stop) is told here, at the session's next such event."""
    if event not in CONTEXT_EVENTS or data.get("agent_id"):
        return False
    tr = context_tracker(where(data)[1])
    if refused := bool(tr and other_format(tr.root)):
        tell_refusal(data, event, tr.root)
    return refused


HOOK_GH_BUDGET_S = 10  # all gh calls of one hook; hooks.json gives the hooks that call gh 15 s


STATUS_OF = {"session-start": "idle", "prompt": "busy", "stop": "idle", "stop-failure": "idle", "session-end": "ended",
             "waiting": "waiting", "resumed": "busy"}  # the session's status after each event


def run_hook(*events: str) -> None:
    """Run each handler that scripts/hook.sh chose for one hook event, in order (after a tool: its own, then
    `resumed`)."""
    data = hook_input()
    budget(HOOK_GH_BUDGET_S)
    event = events[0] if events else ""
    try:
        upgrade_trackers()
        sid = where(data)[1]
        os.environ["TRACKER_SESSION"] = sid  # command_context, session_id() and spawned commands read it
        for event in events:
            status = STATUS_OF.get(event)
            if event == "session-end" and not data.get("agent_id"):
                session_activity(sid, status)  # first: the host may cut a SessionEnd hook short
            try:  # a watch request writes nothing to the tracker: `stop` ends the watch on a refused one too
                if not (event == "prompt" and watch_request(data)) and not refusal_told(data, event) \
                        and HOOKS[event](data) is True:
                    status = None  # a turn the Stop hook held goes on: the session stays busy
            except OtherFormat as exc:  # the guard stopped the first tracker write; session_activity below still runs
                tell_refusal(data, event, exc.root)
            # A subagent's turn is not the session's, but its wait on the user is.
            asker = str(data.get("agent_id") or "") if event in ("waiting", "resumed") else None
            if status and event != "session-end" and (not data.get("agent_id") or asker is not None):
                places = {} if data.get("agent_id") else {  # a subagent's transcript is not the session's
                    "transcript": str(data.get("transcript_path") or ""), "terminal": os.environ.get("HC_TERMINAL_ID")}
                failed = str(data.get("error") or "") if data.get("hook_event_name") == "PostToolUseFailure" else ""
                session_activity(sid, status, asker, places, str(data.get("tool_name") or ""), failed)
    except (Exception, SystemExit) as exc:  # a hook must never break the session; exit 2 would block it
        warn(f"hook {event} failed: {exc}")
        sys.exit(0)
