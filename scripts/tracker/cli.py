"""The plan and progress of a piece of work: tickets, decisions, actions and a log, in Markdown files. `tracker rules`
gives the format, and `tracker <command> --help` a command's syntax.

The `tracker` command line: the parser, the repair of its arguments, and what runs around each write command. The
commands are in `commands`."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .system import die
from .store import first_writes, locked, Busy, OtherFormat
from .schema import (BLOCKER, COORDINATOR, DEFAULT_LABELS, EVENTS, EVIDENCE_DIR, QUIET_KINDS, SCALES, STAGES, STATUSES,
    SUBMIT_WHAT, VERDICTS)
from .model import unblocked, Tracker
from .session import match_cwd, own_change, session_id, watch, work_dir
from .contract import upgrade_trackers
from .views import CHAIN_CARRY_FORWARD_MAX, CONTEXT_LOG, HISTORY_LAST, issue_update_request
from .github import NAME as FORGE
from .watcher import EVENT_KINDS, REFUSED, URGENT
from .ui import GPUI_SHELL_VERSION
from .commands import (cmd_act, cmd_actions, cmd_add, cmd_agents, cmd_archive, cmd_attach, cmd_attached,
    cmd_await_change, cmd_check, cmd_context, cmd_data, cmd_decide, cmd_decisions, cmd_delete, cmd_drop, cmd_evidence,
    cmd_find, cmd_here, cmd_history, cmd_index, cmd_init, cmd_install_mod, cmd_install_window, cmd_issue, cmd_list,
    cmd_log, cmd_new, cmd_open, cmd_put, cmd_ready, cmd_refresh, cmd_review, cmd_rules, cmd_seq, cmd_set, cmd_show,
    cmd_start, cmd_step, cmd_submit, cmd_sync, cmd_unarchive, cmd_use, cmd_wait, cmd_watch, problem_lines, tell_updates,
    tracker_matches)

# ---------------------------------------------------------------- main

IDS = {"action": "extend", "nargs": "+", "metavar": "ID"}  # a comma list, separate words, or both


def build_parser():
    import argparse
    p = argparse.ArgumentParser(prog="tracker", description=__doc__.split("\n\n")[0], epilog="Any text argument but "
                                "a title, a name or a link can be `-`: the text then comes from stdin, as a heredoc "
                                "passes it (<<'EOF').")
    p.add_argument("--tracker", help="tracker slug (default: TRACKER; else the tracker folder the cwd is in, the "
                                     "session's from `tracker start`, or the repo's only tracker)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, listed=True):
        """A command; `listed=False`: one a UI runs, which `tracker --help` leaves out."""
        sp = sub.add_parser(name, **({"help": help_} if listed else {}), description=help_)
        sp.add_argument("--tracker", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("list", cmd_list, "list all trackers, and how many are archived")
    sp.add_argument("--archived", action="store_true", help="list the archived trackers too")
    sp.add_argument("--json", action="store_true", help="every tracker, archived too, as JSON for a UI")
    sp = add("init", cmd_init, "create a tracker")
    sp.add_argument("slug")
    sp.add_argument("--title", required=True)
    sp.add_argument("--owner", required=True)
    sp.add_argument("--repo", help=f"{FORGE} owner/name (comma list if the work spans repos); enables `sync`")
    add("rules", cmd_rules, "the tracker's contract: isolation, files, keys and who writes them, machine state, body "
                            "edits, text limits, statuses, times and value scales, link labels, the decision and "
                            "action bars, and the order, start and move rules")
    sp = add("index", cmd_index, "the work at a glance: the headline with the wait and cycle time lines, the Context "
                                 "links, the user's open actions, one line per ticket, whose move each ticket under "
                                 "way waits on, what can start and the critical path, then open decisions")
    sp.add_argument("--status", help=f"comma list of stages ({'|'.join(STAGES)})")
    sp.add_argument("--active", action="store_true", help="hide closed tickets (done, dropped)")
    sp.add_argument("--group")
    sp = add("here", cmd_here, "the brief for this branch: its handoff, work the tracker may not show yet, its "
                               "tickets under way with what they build on, or what can start")
    sp.add_argument("--full", action="store_true", help="include the tickets' bodies")
    sp = add("context", cmd_context, "a ticket or decision with dependency carry-forward and the decisions "
                                     "that touch it")
    sp.add_argument("id", help="ticket or decision id, an id a ticket's Issue link names, a PR (#123) or a branch")
    sp.add_argument("--brief", action="store_true", help="omit the body")
    sp.add_argument("--deep", action="store_true", help="all carry-forward from earlier in the chain, not the first "
                                                        f"{CHAIN_CARRY_FORWARD_MAX}")
    sp.add_argument("--log", type=int, default=CONTEXT_LOG, metavar="N",
                    help=f"show the last N log lines for it (default {CONTEXT_LOG}; 0 for none)")
    sp = add("decisions", cmd_decisions, "the open decisions and the tickets they touch, and a count of the settled "
                                         "ones; --all adds those, with their answers")
    sp.add_argument("--status", choices=STATUSES["decision"])
    sp.add_argument("--all", action="store_true", help="every decision, settled ones with their answers")
    sp = add("actions", cmd_actions, "the user's open actions: tasks the agent cannot or should not do; --all adds "
                                     "the closed ones")
    sp.add_argument("--all", action="store_true", help="every action, closed ones too")
    sp = add("find", cmd_find, "search tickets, decisions, actions, README and log")
    sp.add_argument("text")
    sp.add_argument("--all", action="store_true", help="search every tracker")
    sp = add("set", cmd_set, "set frontmatter keys that `set` owns (see `rules`): "
                             "set T-8 status=in-progress next=\"...\"; set tracker status=active")
    sp.add_argument("id", nargs="?", help="ticket or decision id, or `tracker` for the work itself; left out: this "
                                          "session's one ticket")
    sp.add_argument("pairs", nargs="+", metavar="key=value")
    sp = add("log", cmd_log, "add a dated note to the log: log \"<what changed and why>\" --ref T-8 D-2")
    sp.add_argument("message", nargs="?")
    sp.add_argument("--ref", **IDS, help="ids it concerns")
    sp = add("add", cmd_add, "add a line to a section of a ticket, decision or the README (`tracker`): "
                             "add T-8 carry \"...\" / add T-8 link \"PR: [title](url) — why\" / "
                             "add T-8 plan \"...\" --replace \"old text\"")
    sp.add_argument("id", help="ticket or decision id, or `tracker` for the README")
    sp.add_argument("section", help="a section, by a prefix or a part of its name: plan, carry, links, question, "
                                    "options, notes, context, goal, scope; a README section it names none of is new")
    sp.add_argument("text", help="the line; a bullet in a list section (Carry forward, Links, Context, Options, "
                                 "Notes). A link "
                                 f"line's label is one of {', '.join(DEFAULT_LABELS)}, or one of the tracker's own "
                                 "(`rules` lists them)")
    sp.add_argument("--replace", metavar="OLD", help="replace this text, which occurs once in the section, instead")
    sp.add_argument("--why", help="why it changes: a log line; needed for the Plan of a started ticket")
    sp = add("put", cmd_put, "replace a whole section of a ticket, a decision or the README (`tracker`) with new "
                             "text, given or from stdin: put T-8 carry - <<'EOF' … EOF")
    sp.add_argument("id", help="ticket or decision id, or `tracker` for the README")
    sp.add_argument("section", help="a section, by a prefix or a part of its name: plan, carry, links, question, "
                                    "options, notes, context, goal, scope; a README section it names none of is new")
    sp.add_argument("text", nargs="?", default="-", help="the section's whole new text; `-` or left out: from stdin")
    sp.add_argument("--why", help="why it changes: a log line; needed for the Plan of a started ticket")
    sp = add("drop", cmd_drop, "remove the one line of a section that holds this text (a bullet goes with its "
                               "nested lines): drop T-8 carry \"old fact\"")
    sp.add_argument("id", help="ticket or decision id, or `tracker` for the README")
    sp.add_argument("section")
    sp.add_argument("text")
    sp.add_argument("--why", help="why it changes: a log line; needed for the Plan of a started ticket")
    sp = add("show", cmd_show, "records' own text by id, whole or only some sections, without the template's "
                               "comments: show T-8 --section carry / show D-01 D-02 --section resolution")
    sp.add_argument("ids", nargs="+", metavar="ID", help="ticket, decision or action ids (a comma list works "
                                                          "too), `tracker` for the README, or `log` for its last lines")
    sp.add_argument("--section", help="comma list of sections, by a prefix of the name: plan, carry, links, question, "
                                      "options, notes, resolution, context, goal, scope")
    sp = add("history", cmd_history, "read the log: its last lines, oldest first, or those about some tickets or "
                                     "decisions, or since a date: history --ref T-8 D-2 --since 2026-10-01")
    sp.add_argument("--ref", **IDS, help="only the lines about these ids")
    sp.add_argument("--since", metavar="YYYY-MM-DD", help="only the lines dated this day or later")
    sp.add_argument("--last", type=int, default=HISTORY_LAST, metavar="N",
                    help=f"the last N lines (default {HISTORY_LAST}; 0 for all)")
    sp.add_argument("--kind", metavar="KINDS", help="only the events of these kinds, comma-separated: "
                    f"{', '.join(EVENTS)}. A wait on a human ({', '.join(QUIET_KINDS)}) shows only when named")
    sp = add("attach", cmd_attach, f"keep a file in the tracker's {EVIDENCE_DIR}/ folder and link it from the "
                                   "tickets and decisions it supports")
    sp.add_argument("file", nargs="?")
    sp.add_argument("--ref", **IDS, help="tickets (a Links line) and decisions (an Options line) it supports")
    sp.add_argument("--note", help="what it shows")
    sp.add_argument("--name", help=f"its file name in {EVIDENCE_DIR}/ (default: the file's own)")
    sp.add_argument("--force", action="store_true", help="replace a file of that name with other content")
    sp.add_argument("--append", metavar="TEXT", help=f"add the text (`-`: from stdin) at the end of the file named, "
                                                   f"which {EVIDENCE_DIR}/ holds already")
    sp = add("new", cmd_new, "create a ticket from its template (decisions: `decide`; actions: `act`)")
    sp.add_argument("id", nargs="?")
    sp.add_argument("--title", required=True)
    sp.add_argument("--group", help="a free label that groups tickets in the views; order comes from depends_on")
    sp.add_argument("--branch")
    sp.add_argument("--repo", help="the ticket's repo, when the tracker spans several")
    sp.add_argument("--depends", **IDS, help="ticket, decision or action ids it waits on")
    sp.add_argument("--next")
    sp.add_argument("--plan", metavar="TEXT", help="the ticket's Plan as agreed: goal, scope, acceptance criteria "
                                                   "(`-`: from stdin)")
    for key, scale in SCALES.items():
        sp.add_argument(f"--{key}", help=f"{scale.span} ({scale.ends}); with an Issue link, the issue's own")
    sp.add_argument("--due", metavar="YYYY-MM-DD", help="the day it should be done by, only one the user or its "
                                                       "issue tracker gave")
    sp = add("decide", cmd_decide, "open a direction decision (by title), or update / resolve one (by D-id)")
    sp.add_argument("target", nargs="?", help="a new decision's title, or an existing D-id")
    sp.add_argument("--question", help="what must be decided and why it matters (new: defaults to the title)")
    sp.add_argument("--option", action="append", metavar="CHOICE", help="a choice to decide between, `<choice>` or "
                                                                        "`<choice> — <what it costs>`; one per choice")
    sp.add_argument("--recommend", metavar="CHOICE", help="the choice you recommend; added when it is new")
    sp.add_argument("--note", help="a fact, proposal or finding; a dated line in ## Notes")
    sp.add_argument("--resolve", help="the answer; closes the decision")
    sp.add_argument("--by", metavar="NAME", help="with --resolve: the human who decided")
    sp.add_argument("--source", metavar="WHERE", help="with --resolve: where the answer already is, when no human "
                                                      "decided it (the spec, a settled decision's id, a file)")
    sp.add_argument("--refs", **IDS, help="ticket ids it touches but does not block")
    sp.add_argument("--unref", **IDS, help="ticket ids to drop from refs")
    sp.add_argument("--blocks", **IDS, help="ticket ids that cannot proceed until it is settled (adds it to their "
                                            "depends_on)")
    sp.add_argument("--owner", help="who must decide")
    sp.add_argument("--force", action="store_true", help="open even though a similar decision is open")
    sp = add("act", cmd_act, "add a task for the user that the agent cannot or should not do (by its text), or note "
                             "on / close one (by A-id): act \"Ask Sam whether v1 stays\" --refs T-3 / act A-01 --done")
    sp.add_argument("target", nargs="?", help="what the user must do, and with whom; or an existing A-id")
    sp.add_argument("--refs", **IDS, help="ticket or decision ids it concerns but does not block")
    sp.add_argument("--unref", **IDS, help="ids to drop from refs")
    sp.add_argument("--blocks", **IDS, help="ticket ids that cannot proceed until it is done (adds it to their "
                                            "depends_on)")
    sp.add_argument("--note", action="append", help="one fact: the context (what to ask or say, and why), or the "
                                                   "reply or outcome; a line in the action. Repeat it for each "
                                                   "fact")
    sp.add_argument("--due", metavar="YYYY-MM-DD", help="the day the user should do it by, only one they gave; "
                                                       "`none` removes it")
    sp.add_argument("--title", help="a new text for the action (by A-id)")
    sp.add_argument("--done", action="store_true", help="the user did it; closes the action")
    sp.add_argument("--drop", action="store_true", help="no longer needed; closes the action")
    sp.add_argument("--force", action="store_true", help="add even though a similar action is open")
    sp = add("wait", cmd_wait, "record what a ticket waits on, or remove it: wait T-14 on T-7 D-10 A-02 / "
                               "wait T-6 on X-1 --link \"<url> — why\" / wait T-14 off D-10")
    sp.add_argument("id", help="the ticket that waits")
    sp.add_argument("mode", choices=["on", "off"])
    sp.add_argument("items", nargs="+", help="ticket, decision or action ids, or an external blocker's id")
    sp.add_argument("--link", help=f"for a new external blocker: \"<url> — <why it blocks>\"; adds a "
                                   f"`- {BLOCKER}:` line to ## Links")
    sp = add("seq", cmd_seq, "the order: step, waits on and unblocks per ticket; ready list; critical path")
    sp.add_argument("--active", action="store_true", help="hide closed tickets")
    add("ready", cmd_ready, "the todo tickets that can start now, the most urgent first: nothing blocks them (from "
                            "the default branch), or they wait only on tickets under way and stack on the branch "
                            "named; with their priority and size and what each unblocks")
    add("check", cmd_check, "validate the tracker, then list what is stuck (a submission or a question left too long "
                            "or on a ticket under way that no session is on, a wait on a human past 30 s); exit 1 on "
                            "errors")
    sp = add("sync", cmd_sync, f"pull each ticket's PRs (number, state, merge time) from {FORGE}, and the open PRs' "
                               "base, reviews, checks and merge state, from which each ticket's move is computed")
    sp.add_argument("--live", action="store_true", help="only if no sync ran lately: what a UI runs on a timer")
    sp = add("issue", cmd_issue, "record a ticket's issue fields as read from its issue tracker (priority, size, "
                                 "due day, when the issue was created), or with --written that its issue now shows "
                                 "the tracker's changes; with no id, list the tickets whose fields are to read and, "
                                 "with README issue_updates on, the updates to write")
    sp.add_argument("id", nargs="?", help="ticket id, or an id its Issue link names")
    for key, scale in SCALES.items():
        sp.add_argument(f"--{key}", help=f"the issue's {key} mapped onto {scale.span} ({scale.ends}); empty: it "
                                         f"has none")
    sp.add_argument("--due", metavar="YYYY-MM-DD", help="the issue's due day; empty: it has none")
    sp.add_argument("--created", help="when the issue was created, ISO 8601 with a time zone (2026-10-01T09:30:00Z)")
    sp.add_argument("--written", action="store_true", help="every update `tracker issue` last listed for the "
                                                           "ticket is written to its issue; a change since stays due")
    sp = add("step", cmd_step, "record a step in one command: the next action (or --done), Carry forward and a log "
                               "line; then, for a ticket on this branch, log the commits the hooks have not (they log "
                               "each commit on a branch with a ticket under way), mark it at HEAD and clear its "
                               "handoff (with --pause, leave one for the next session, which its brief shows first). "
                               "With no arguments, only that last part, for the current branch")
    sp.add_argument("id", nargs="?", help="the ticket; left out: this session's one ticket")
    sp.add_argument("message", nargs="?", help="what changed and why that the commit subjects do not say (the hooks "
                                              "log those): a log line")
    sp.add_argument("--next", help="the next action, true as of now")
    sp.add_argument("--carry", action="append", metavar="FACT", help="a fact a later ticket must know (repeatable)")
    sp.add_argument("--done", metavar="SUMMARY", help="close the ticket: what it delivered")
    sp.add_argument("--pause", metavar="HANDOFF", help="stopping mid-work: what is done, what is half-done and "
                                                       "uncommitted, and the next step")
    sp = add("submit", cmd_submit, "hand a ticket's plan, or its built code, to its reviewer (the coordinator, else "
                                   "a human), whose review it waits on until `tracker review` answers it")
    sp.add_argument("id", help="the ticket")
    sp.add_argument("--what", required=True, choices=SUBMIT_WHAT, help="plan: its plan is ready for review; code: "
                                                                      "its built code is ready for the plan "
                                                                      "conformance check")
    sp.add_argument("--note", help="what the reviewer should know, such as where the plan is")
    sp = add("review", cmd_review, "record a review of a ticket's plan or built code, with its numbered findings: "
                                   "review T-8 --what plan --verdict changes --finding \"...\" --finding \"...\"; or "
                                   "mark findings of the latest review fixed: review T-8 --what plan --fixed 2,3")
    sp.add_argument("id", help="the ticket")
    sp.add_argument("--what", required=True, choices=SUBMIT_WHAT, help="what was reviewed: its plan, or its built "
                                                                      "code (the plan conformance check)")
    sp.add_argument("--verdict", choices=VERDICTS, help="pass, or changes: the changes it asks for, each a --finding")
    sp.add_argument("--finding", action="append", metavar="TEXT", help="one change the review asks for; repeat it "
                                                                     "for each, numbered in that order")
    sp.add_argument("--fixed", metavar="N,N", help="the numbers of the latest review's findings now fixed, "
                                                   "comma-separated")
    sp = add("start", cmd_start, "tie a tracker to this agent session, by its slug or words of its title "
                                 "(`start payments rework`); prints the brief. Lists options when unsure (exit 3). "
                                 "Never changes git")
    sp.add_argument("name", nargs="*", help="the tracker's slug, or words of its slug or title; none: the tracker "
                                            "with an open ticket on this branch (in a session with a tracker: its "
                                            "brief)")
    sp.add_argument("--on", **IDS, help="the ticket(s) this session works on, when its branch holds more: the brief, "
                                        "the hooks, and `step` and `set` without an id use them alone. After the "
                                        "name; `tracker start <slug>` alone clears it")
    sp.add_argument("--clear", action="store_true", help="tie this session to no tracker")
    sp.add_argument("--decline", action="store_true", help="\"Not now\": unlink this session, and no link or offer "
                                                           "of a tracker on this branch for a day")
    sp = add("use", cmd_use, "put ticket(s) on the current branch: sets each one's branch (the branch keeps its "
                             "other tickets); on a shared branch such as main, records the choice for this worktree "
                             "only")
    sp.add_argument("ids", nargs="*", help="ticket ids")
    sp.add_argument("--clear", action="store_true", help="forget this worktree and branch's choice")
    sp = add("watch", cmd_watch, "print each change to a tracker, one line per ticket or agent (`!` marks what needs "
                                 "a human), until stopped: a move, a step, a decision, a PR event, an agent that ends "
                                 "its turn or waits on the user, a `check` error, a stuck item. One watch per session "
                                 "and tracker. In an agent session, run it only when the user, or a skill or "
                                 "instruction from them, tells you to watch: never on your own")
    sp.add_argument("name", nargs="*", help="the tracker's slug, or words of its slug or title")
    sp.add_argument("--once", action="store_true", help="print the first batch (at the first run: the state now), "
                                                        "then exit")
    sp.add_argument("--only", metavar="KINDS", help="print only these kinds of event, comma-separated: "
                    + "; ".join(f"{k} ({what})" for k, what in EVENT_KINDS.items())
                    + f"; {URGENT} (any marked `!`)")
    sp.add_argument("--stop", action="store_true", help="end this session's watch: a running `tracker watch` of it "
                                                        "stops within seconds")
    sp.add_argument("--hold", type=int, default=0, metavar="SECONDS",
                    help="hold events not marked `!` until the oldest is this old, then print them as one batch; one "
                         "marked `!` prints at once, with those held")
    sp.add_argument("--as", dest="role", choices=[COORDINATOR], help="in an agent session: take the tracker's "
                    "coordinator role while this watch runs; an open decision with no owner is then yours to triage "
                    "before a human's")
    sp = add("agents", cmd_agents, "the live agent sessions on a tracker, one line each: name, status and for how "
                                   "long, tickets, branch, folder, Claude Code session name, Herdr terminal and "
                                   "transcript file")
    sp.add_argument("name", nargs="*", help="the tracker's slug, or words of its slug or title")
    sp = add("archive", cmd_archive, "put a tracker away: out of every list, lookup and hook, still in the UI. "
                                     "Refuses while another agent session or a watch is on it")
    sp.add_argument("slug", help="the tracker's exact slug")
    sp = add("unarchive", cmd_unarchive, "bring an archived tracker back")
    sp.add_argument("slug", help="the archived tracker's exact slug")
    sp = add("delete", cmd_delete, "for the user: move a tracker's folder, archived or not, to the system's trash. "
                                   "Refuses while an agent session or a watch is on it, and in an agent session")
    sp.add_argument("slug", help="the tracker's exact slug")
    sp.add_argument("--yes", action="store_true", help="delete without the prompt")
    sp = add("data", cmd_data, "all a UI shows of a tracker, as JSON", listed=False)
    sp.add_argument("slug", nargs="?", help="tracker slug (an archived one too)")
    sp = add("await-change", cmd_await_change, "wait until what a UI shows of a tracker may have changed", listed=False)
    sp.add_argument("slug", nargs="?", help="tracker slug (an archived one too)")
    sp = add("attached", cmd_attached, "the tracker linked to the agent in a process, as JSON, for a UI", listed=False)
    sp.add_argument("--pid", type=int, required=True, help="the agent's process id")
    sp.add_argument("--cwd", default="", help="its folder: finds a host that keeps no process file")
    sp = add("open", cmd_open, "show the tracker in its UI: a tab in Herdr when Herdr is open with the mod, else its "
                                  "own window")
    sp.add_argument("id", nargs="?", help="ticket, decision or action to open, or another tracker by its slug "
                                             "(an archived one too)")
    sp.add_argument("--window", action="store_true", help="its own window, also when Herdr is open")
    add("install-window", cmd_install_window, f"download gpui-shell {GPUI_SHELL_VERSION} for this system, which the "
                                              "UI's own window needs")
    add("install-mod", cmd_install_mod, "install the UI as a Herdr mod, which runs this CLI; later plugin versions "
                                        "update it")
    add("refresh", cmd_refresh, "pull PR state now, and have the next session prompt read the issue fields again: a "
                                "UI's Refresh", listed=False)
    sp = add("evidence", cmd_evidence, "open a file of the tracker's evidence/ on the user's screen: a UI's link",
             listed=False)
    sp.add_argument("file", help="its path under evidence/")
    return p, sub


WRITE_COMMANDS = {"init", "set", "log", "new", "decide", "act", "wait", "step", "use",
                  "add", "put", "drop", "attach", "issue", "submit", "review"}  # `sync` locks itself


def with_check(run, args) -> None:
    """Run a write command, then print the tickets it left unblocked, the request for an issue update it made due,
    and the `check` problems it added to the tracker, and end with whether `check` found new problems: no
    `tracker check` needs to follow a write. Each tracker the command writes is compared with itself as it was before
    its first write (`first_writes`), so a write to another tracker (`slug:ID`, an id found in any tracker) reports on
    that one."""
    if args.cmd == "init":
        run()
        return
    before = {}

    def snapshot(root: Path) -> None:
        tr = Tracker(root)
        before[root] = (set(problem_lines(tr)), {t.id for t in tr.tickets if tr.blockers(t)}, issue_update_items(tr))

    with first_writes(snapshot):
        run()
    for root, (problems, blocked, due) in before.items():
        tr = Tracker(root)
        added = [x for x in problem_lines(tr) if x not in problems]
        lines = unblocked(tr, blocked)
        if issue_update_items(tr) - due:  # this write made an issue update due: ask for it now
            lines.append(issue_update_request(tr))
            tell_updates(tr)
        if added:
            lines += ["This change added `tracker check` problems:", *added]
        lines.append("`check`: " + ("fix the problems above" if added else "no new problems"))
        print("\n".join(lines))


def issue_update_items(tr: Tracker) -> set[tuple[str, str]]:
    return {(t.id, x) for t, items in tr.issue_updates_due() for x in items}


def own_changes(run) -> None:
    """Run a write command, and count what it changed about the session's tickets as seen: the prompt hook reports
    only what others changed."""
    sid = session_id()
    m = match_cwd(work_dir(), sid) if sid else None
    before = watch(m.tracker, m.focus) if m and m.focus else None
    run()
    if before is not None:
        own_change(sid, m, before)


# The positional that an id list can swallow: `log --ref A B "msg"` gives the list "msg" too, as argparse's usage line
# puts options first. The list's last word goes back to the positional.
AFTER_IDS = {"log": "message", "attach": "file", "decide": "target", "act": "target", "new": "id"}
IDS_DESTS = ("ref", "refs", "unref", "blocks", "depends")


# The text arguments that take `-`: the text then comes from stdin, so a heredoc (<<'EOF') passes quotes, backticks
# and lines as they are. `set` takes `key=-`.
TEXT_DESTS = ("message", "text", "next", "plan", "done", "pause", "carry", "question", "note", "resolve", "replace",
              "append", "why", "pairs", "finding")


def no_id(args) -> None:
    """`set next=...` and `step "<what and why>"` leave the id out, for this session's ticket: the first word is then a
    pair or the message."""
    if args.cmd == "set" and args.id and "=" in args.id:
        args.pairs.insert(0, args.id)
        args.id = None
    if args.cmd == "step" and args.id and not args.message and (args.id == "-" or re.search(r"\s", args.id)):
        args.id, args.message = None, args.id


def stdin_text(args) -> None:
    if args.cmd == "find":  # its text is a search
        return
    spots = []
    for dest in TEXT_DESTS:
        value = getattr(args, dest, None)
        if value == "-":
            spots.append((dest, None))
        elif isinstance(value, list):
            spots += [(dest, i) for i, v in enumerate(value) if v == "-" or (dest == "pairs" and v.endswith("=-"))]
    if not spots:
        return
    if len(spots) > 1:
        die("only one text can come from stdin (`-`)")
    if sys.stdin is None or sys.stdin.isatty():
        die("`-` reads the text from stdin, and none is piped: use a heredoc (<<'EOF')")
    text, (dest, i) = sys.stdin.read().strip("\n"), spots[0]
    if i is None:
        setattr(args, dest, text)
    else:
        values = getattr(args, dest)
        values[i] = values[i][:-1] + text if dest == "pairs" else text


def reclaim(args, argv: list[str], parser) -> None:
    name = AFTER_IDS.get(args.cmd)
    if not name or getattr(args, name):
        return
    last = max(((i, d) for i, tok in enumerate(argv) for d in IDS_DESTS
                if tok == f"--{d}" and getattr(args, d, None)), default=None)
    if last:
        setattr(args, name, getattr(args, last[1]).pop())
    if not getattr(args, name):
        parser.error(f"{args.cmd}: the following arguments are required: {name}")


ALIASES = {"view": "open"}  # a word the model reaches for: the command it means


def named_command(argv: list[str], commands) -> list[str]:
    """The skill runs `tracker $ARGUMENTS`, so a slash command's words arrive as they were typed: `view` means `open`,
    and words that name a tracker but no command mean `start` (`/work-tracker:tracker payments`)."""
    i = 2 if argv[:1] == ["--tracker"] else 1 if argv[:1] and argv[0].startswith("--tracker=") else 0
    head, rest = argv[:i], argv[i:]
    if not rest or rest[0].startswith("-") or rest[0] in commands:
        return argv
    if rest[0] in ALIASES:
        return [*head, ALIASES[rest[0]], *rest[1:]]
    return [*head, "start", *rest] if tracker_matches(rest)[0] else argv


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser, commands = build_parser()
    argv = named_command(argv, commands.choices)
    args = parser.parse_args(argv)
    reclaim(args, argv, parser)
    no_id(args)
    stdin_text(args)
    upgrade_trackers()
    try:
        if args.cmd in WRITE_COMMANDS:
            with locked():
                own_changes(lambda: with_check(lambda: args.fn(args), args))
        else:
            args.fn(args)
    except Busy as exc:
        die(str(exc))
    except OtherFormat as exc:
        die(str(exc), REFUSED)
