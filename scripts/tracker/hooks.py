"""Agent hooks: scripts/hook.sh runs `run_hook(<event>)` with the hook JSON on stdin, after its shell filters."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from .model import ISOLATION_RULE, SYNC_MIN_INTERVAL_S, append_log, locked, short, spawn, today, Tracker
from .git import default_branches
from .session import (behind, branch_matches, changes_since, declined, get_mark, in_repos, inside_home, lag,
    load_session, match_cwd, own_edit, record_commits, remember, save_session, session_activity, watch, work_dir,
    Lag, Match)
from .views import brief, issue_request
from .github import budget, match_pr, sync
from .watcher import set_grant

PR_TRIGGER = re.compile(r"\bgh\s+pr\s+(create|merge|ready|close|reopen|edit)\b|\bgit\s+push\b")

# ---------------------------------------------------------------- hooks
# scripts/hook.sh filters each event before Python starts: a session with no tracker exits after one file test; an
# edit starts Python only for a tracker file; a Bash command only when it may commit, push or run `gh pr`. The work
# the tracker may not show comes from git (`lag`), so edits by any tool or subagent count. A hook adds context only
# when it changes what the AI should do next, and says each thing once.


def hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}


def emit_context(event: str, text: str, notice: str = "") -> None:
    """`text` goes to the model; `notice`, if any, is shown to the user in the transcript."""
    if event in ("SessionStart", "SubagentStart"):
        text = "\n".join(x for x in (command_context(), text) if x)
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    print(json.dumps(out | ({"systemMessage": notice} if notice else {})))


def command_context() -> str:
    """Hosts without a shell env file get the CLI path and the hook's authoritative session id in context."""
    root, sid = os.environ.get("PLUGIN_ROOT"), os.environ.get("TRACKER_SESSION")
    if not root or not sid or os.environ.get("CLAUDE_ENV_FILE"):
        return ""
    command = f"env TRACKER_SESSION={shlex.quote(sid)} {shlex.quote(str(Path(root) / 'bin/tracker'))}"
    return f"[work-tracker] For every tracker command in this session, use `{command}` as the command prefix."


def offer(found: list[Match]) -> tuple[str, str]:
    """For a session with no tracker on a branch with open tickets: the model's request to ask the user whether to
    link it, and the line the user sees at once (a hook cannot start a model turn in an interactive session, so the
    model asks at the first message). Only the user's answer links; the hook writes nothing."""
    names = "; ".join(f"{m.tracker.slug} ({m.tracker.title}): {m.summary()}" for m in found[:3])
    options = [f'"Link {m.tracker.slug}" (run `tracker start {m.tracker.slug}`, which prints the brief)'
               for m in found[:3]]
    ask = (f"[work-tracker] This session is on no tracker. Branch `{found[0].branch}` has open tickets in {names}.\n"
           "In your first reply, before other work, use the host's question tool to ask whether to link this session "
           f"to it. Options: {', '.join(options)}, and \"Not now\" (run `tracker start --decline`: no offer on this "
           "branch for a day). Skip the question when the user's message already answers it.")
    return ask, (f"[work-tracker] Branch `{found[0].branch}` has open tickets in {names}. The agent asks at your first "
                 "message whether to link this session.")


def hook_session_start(data: dict) -> None:
    cwd = data.get("cwd") or os.getcwd()
    sid = str(data.get("session_id") or "")
    m = match_cwd(cwd, sid)
    if not m:
        # CLAUDE_CODE_SESSION_ATTENDED: 1 when a person uses the session, 0 when no one can answer (`claude -p`).
        unattended = os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0"
        asked = data.get("source") == "compact" or unattended or declined(cwd)  # asked once; or no one can answer
        found = [] if asked else branch_matches(cwd)
        if found:
            emit_context("SessionStart", *offer(found))
        elif command_context():
            emit_context("SessionStart", "")
        return
    changes = sync(m.tracker, force=False)
    m, found = match_pr(match_cwd(cwd, sid), cwd)  # reload after sync
    record_commits(m, cwd)  # made since the last session, as in the terminal; or the branch's baseline
    remember(sid, m, cwd)
    emit_context("SessionStart", brief(m, cwd, changes, found, compact=data.get("source") == "compact"))


def adopt_branch(m: Match, cwd: str | Path) -> None:
    """A branch that holds one ticket's id in its name (made when the user asked): record it on the ticket, where
    sync finds its PR and every worktree finds the ticket."""
    if m.how != "name" or len(m.tickets) != 1 or m.branch in default_branches(cwd):
        return
    with locked():
        tr = Tracker(m.tracker.root)
        t = tr.lookup(m.tickets[0].id)
        if t and not t.get("branch"):
            t.save({"branch": m.branch, "updated": today()})
            append_log(tr, f"{t.id} is built on branch {m.branch}", [t.id])


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
            "a step ended or `next` no longer holds: `tracker step <id> --next \"...\"`, with a message only for what "
            "the commits do not say, and `--carry \"...\"` for each fact a later ticket must know.")


def hook_stop(data: dict) -> None:
    """The end of a turn: log the commits no other hook saw (made in the terminal, or by a command the post-bash filter
    let through). Never blocks: the model records only what git does not hold, when `next_request` asks."""
    cwd = data.get("cwd") or os.getcwd()
    m = match_cwd(cwd, str(data.get("session_id") or ""))
    if not m or not m.branch:
        return
    adopt_branch(m, cwd)
    record_commits(m, cwd)


COMMIT_TRIGGER = re.compile(r"\bgit\b[^|;&\n]*\bcommit\b")


def hook_post_bash(data: dict) -> None:
    """After `gh pr ...` or a push: sync PR state. After a commit: log it, and when it is the first since `next` last
    changed, ask whether a step ended, while the work is fresh. A subagent's commit (its tool calls fire these hooks
    too) is logged, but the subagent is asked nothing: the session records the step, asked at its next message."""
    subagent = bool(data.get("agent_id"))
    command = (data.get("tool_input") or {}).get("command", "")
    pr, commit = PR_TRIGGER.search(command), COMMIT_TRIGGER.search(command)
    if not (pr or commit):
        return
    cwd = data.get("cwd") or os.getcwd()
    sid = str(data.get("session_id") or "")
    m = match_cwd(cwd, sid)
    if not m:
        return
    out = []
    if pr:
        adopt_branch(m, cwd)
        changes = sync(m.tracker, force=True)
        if changes and not subagent:
            out.append("[work-tracker] Synced from GitHub: " + "; ".join(changes))
    if commit:
        record_commits(m, cwd)
        if not subagent and (ask := next_request(m, cwd, sid)):
            out.append(ask)
    if out:
        emit_context("PostToolUse", "\n".join(out))


def hook_edit(data: dict) -> None:
    """hook.sh starts this for a hand edit of a tracker file: count its change as this session's, so the next message
    does not report it as another session's."""
    sid = str(data.get("session_id") or "")
    cwd = data.get("cwd") or os.getcwd()
    m = match_cwd(work_dir(cwd), sid)
    tool = data.get("tool_input") or {}
    paths = [tool["file_path"]] if tool.get("file_path") else re.findall(
        r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", tool.get("command", ""), re.M)
    for path in paths:
        edited = (Path(cwd) / path).resolve()
        if m and m.focus and edited.is_relative_to(m.tracker.root.resolve()):
            own_edit(sid, m, edited.stem)


NUDGE_EVERY = max(1, int(os.environ.get("TRACKER_NUDGE_EVERY", "5")))


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


def refresh(m: Match, entry: dict, fields: dict) -> None:
    """With tickets under way, pull GitHub in the background once the last sync is older than SYNC_MIN_INTERVAL_S (at
    most that often per session): a later message then reports what it changed, such as a review that makes a move
    yours. This message does not wait for it."""
    now = time.time()
    last = max(m.tracker.state().get("last_sync", 0), entry.get("sync_asked", 0))
    if not m.active or not m.tracker.repos or now - last < SYNC_MIN_INTERVAL_S:
        return
    fields["sync_asked"] = int(now)
    try:
        spawn("--tracker", m.tracker.slug, "sync")
    except OSError:  # the message's own lines still go out; the next sync is at the next session or PR command
        pass


WATCH_PROMPT = re.compile(r"[/$](?:work-tracker:)?watch\b(.*)", re.S)


def watch_request(data: dict) -> bool:
    """The user typed /work-tracker:watch: give this session the watch (`tracker watch` then runs in it), or end it
    (`stop`). Only a prompt the user types fires this hook, so the model cannot give itself one. A session on a
    tracker does the work, so it does not watch."""
    m = WATCH_PROMPT.fullmatch(str(data.get("prompt") or "").strip())
    sid = str(data.get("session_id") or "")
    if not m or not sid:
        return False
    stop = m[1].split() == ["stop"]
    own = load_session(sid).get("tracker")
    if own and not stop:
        emit_context("UserPromptSubmit", f"[work-tracker] This session works on tracker {own}, so it cannot watch: "
                     "tell the user to type /work-tracker:watch in a new session.")
    else:
        set_grant(sid, not stop)
    return True


def hook_prompt(data: dict) -> None:
    """On tracked work, each user message gets what other sessions or GitHub changed since the session's brief, and
    `next_request` when commits (a subagent's too) passed `next`; the first message and every NUDGE_EVERY-th after it
    get the state line, so it stays a reminder, not noise. Work since the branch's mark with no ticket in progress
    brings the state line at once, once per mark. A stale GitHub sync starts in the background (`refresh`)."""
    if watch_request(data):
        return
    sid = str(data.get("session_id") or "")
    cwd = data.get("cwd") or os.getcwd()
    m = match_cwd(cwd, sid)
    if not m:
        return
    entry = load_session(sid)
    n = entry.get("prompts", 0)
    ids = [t.id for t in m.focus]
    now = watch(m.tracker, m.focus)
    # A brief on other tickets (the branch changed) is no baseline: start again from now.
    news = changes_since(m.tracker, entry["seen"], now) if entry.get("seen_for") == ids and ids else []
    fields = {"prompts": n + 1, "seen": now, "seen_for": ids} | ({} if inside_home(cwd) else {"cwd": str(cwd)})
    lines = []
    if news:
        lines.append(f"[work-tracker] Changed since your brief on {', '.join(ids)} (by another session or "
                     f"GitHub): " + "; ".join(news) + ".")
    ask = next_request(m, cwd, sid)
    if ask:
        lines.append(ask)  # it holds what the state line would say
    asked = m.tracker.raw_state().get("issues", {}).get("requested", 0)
    if asked and asked != entry.get("told_issues"):  # the viewer's Refresh: passed on once
        fields["told_issues"] = asked
        lines += [x for x in [issue_request(m.tracker)] if x]
    idle = not m.active and bool(m.branch) and in_repos(m.tracker, cwd)
    work = lag(m, cwd) if not ask and (n % NUDGE_EVERY == 0 or idle) else None
    new = work is not None and bool(work.commits or work.files)
    if work is not None and (n % NUDGE_EVERY == 0 or (new and entry.get("idle_told") != work.since)):
        lines.append(state_line(m, work))
        if idle and new:
            fields["idle_told"] = work.since  # said once per mark
    refresh(m, entry, fields)
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
    """The user answered a question the AI asked: an answer that settles direction is a decision made. A yes or no to
    go ahead settles none."""
    if approval_only(data.get("tool_input") or {}) or data.get("agent_id"):
        return
    if match_cwd(data.get("cwd") or os.getcwd(), str(data.get("session_id") or "")):
        emit_context("PostToolUse", "[work-tracker] If the answer settles a direction decision, record it: "
                     "`tracker decide D-<n> --resolve \"...\" --by <who>`, or `tracker decide \"<title>\" --resolve "
                     "\"...\" --by <who>` for one made on the spot.")


def hook_subagent_start(data: dict) -> None:
    """A subagent of a session on a tracker reads it, but leaves the writing to the session, which sees the whole turn
    and records it once."""
    m = match_cwd(data.get("cwd") or os.getcwd(), str(data.get("session_id") or ""))
    if m:
        ids = ", ".join(t.id for t in m.focus)
        emit_context("SubagentStart", f"[work-tracker] The session that started you works on tracker {m.tracker.slug}"
                     + (f", ticket {ids}" if ids else "") + ". Read it when you need to (`tracker context <id>`, "
                     "`tracker show <id> --section <name>`), but do not write it: run no `tracker` command that "
                     "changes it, and edit no tracker file. The hooks log your commits; put what else it should "
                     "record (a finished step, a fact a later ticket needs, a decision, a blocker) in your final "
                     f"answer: the session records it. {ISOLATION_RULE}")


HOOKS = {"session-start": hook_session_start, "stop": hook_stop, "session-end": hook_stop,
         "post-bash": hook_post_bash, "edit": hook_edit,
         "prompt": hook_prompt, "answered": hook_answered, "subagent-start": hook_subagent_start}


HOOK_GH_BUDGET_S = 10  # all gh calls of one hook; hooks.json gives the hooks that call gh 15 s


def run_hook(event: str) -> None:
    """What scripts/hook.sh runs for an event, with the hook JSON on stdin."""
    data = hook_input()
    budget(HOOK_GH_BUDGET_S)
    try:
        os.environ["TRACKER_SESSION"] = str(data.get("session_id") or "")
        HOOKS[event](data)
        status = {"session-start": "idle", "prompt": "busy", "stop": "idle", "session-end": "ended"}.get(event)
        if status and not data.get("agent_id"):
            session_activity(str(data.get("session_id") or ""), status)
    except (Exception, SystemExit) as exc:  # a hook must never break the session; exit 2 would block it
        print(f"work-tracker hook {event} failed: {exc}", file=sys.stderr)
        sys.exit(0)
