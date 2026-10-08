---
name: tracker
description: Work tracker - the local plan and progress record for a piece of work (tickets, decisions, log). Use when the user says to start or work on a tracker or epic by name, when a `[work-tracker]` block is in context, before starting, after finishing or before pausing work on a tracked ticket, when a decision is made or opened, or when the user asks to create, view, migrate or update a tracker.
argument-hint: "[tracker command, e.g. index --active]"
---

# Work tracker

A tracker is the single record of a piece of work's plan and state: the **hand-off** to the next session, which starts with no memory of this one, and the page the user watches (`tracker open`). It lives in `$TRACKER_HOME` (default `~/.claude/trackers/<slug>/`), outside every repo, so every worktree and session reads one copy. It is **isolated**: the isolation rule, first in the brief's protocol and in `tracker rules`, governs every text you write outside it.

The CLI is `tracker`; `tracker <command> --help` gives each command's syntax. If this skill is invoked with arguments, run `tracker $ARGUMENTS` and report the result; with none, run `tracker start`.

Use the command prefix from the session's `[work-tracker]` hook for every call when supplied. If `tracker` is absent from PATH, use the absolute path to `bin/tracker` at this plugin's root (two directories above this file).

## Model

| File | Holds |
|---|---|
| `README.md` | the work: Context (the documents it answers to), Goal, Scope; ≤ ~2K tokens, rewritten, never appended to |
| `tickets/<ID>.md` | frontmatter = state (`status`, `branch`, `next`, `summary`, `depends_on`, PR keys, issue keys); body = Plan, Carry forward, Links |
| `decisions/D-<n>.md` | a direction decision: Question, Options, and once closed, Resolution |
| `log.md` | dated one-line history, append-only |
| `evidence/` | files the records cite: runs, measurements, scripts |
| `.state.json` | machine state, never edited: per repo and branch the mark up to which the hooks logged its commits, the handoff and PR lookups; per worktree the `use` choice. Stale entries go by themselves |

**One source per fact.** Each fact has one home; every view is computed from it, so write it there once:

- **Order and blockers**: the waiting ticket's `depends_on`; an open PR based on another ticket's branch also waits on it (from the PR, not written). Ready, blocked, unblocks and the critical path are computed.
- **Where a ticket starts**: computed from the same. A todo ticket that waits only on tickets under way can start stacked on their branch; `tracker ready` and `context` name it. Do not write the base into the Plan, `next` or the log.
- **A branch's tickets**: each ticket's `branch`. A branch holds any number; the session works on the ones under way (in progress or in review).
- **PR state**: written by `sync`. Once a ticket is in progress, its PR shows it in review or merged. You set only the work status.
- **Whose move**: computed for each ticket under way from its PR's reviews, checks and merge state (`sync`) and its open decisions and external blockers; the brief's `move:` line, `tracker index` and the viewer show it. `next` holds your own next action: do not write a wait on a reviewer into it.
- **A decision's answer**: its Resolution. Tickets show it through `context`.
- **Issue-tracker id**: the ticket's `- Issue: [PROJ-12 Title](url)` line; the id also finds the ticket.
- **Issue fields** (`priority`, `issue_created`): the issue tracker holds them; `tracker issue` records what you read there. The tracker cannot read an issue tracker, so the brief, or a prompt after the viewer's Refresh, names the tickets whose fields are due.
- **Wait and cycle time**: computed from each ticket's times. Wait: issue created → started (`started_at`, which `set status=in-progress` records). Cycle: started → PR merged. `tracker index` (the lines under its headline) and the viewer show them: quote them, do not work them out from the files.
- **Unfinished work between sessions**: the branch's handoff, until the next `step`.
- **Build detail**: the PR and commits. The hooks log each commit on the branch of a ticket under way: do not log a commit again.

Link lines (README Context, ticket Links) read `- Label: [title](url) — why it matters`. When a Context document governs a choice you are making, open it and work from it.

`tracker rules` is the full contract: every key and what writes it, the statuses, labels, sections and the decision bar. Read it before you edit a tracker file by hand, and when a command refuses a key or a label. `tracker check` enforces it.

## Read

1. **Which work.** `tracker start <name words>` ties the tracker the user names to this session (its hooks and commands then use it) and prints the brief; `tracker start` alone takes the tracker with an open ticket on this branch. When it lists options (exit 3), ask the user which one. Each new session, after `/clear` too, starts on no tracker; on a branch with an open ticket, a `[work-tracker]` line at its start asks you to offer the link. Link only on the user's yes; on "Not now", run `tracker start --decline` (no offer on this branch for a day). `--tracker <slug>` works for one command.
2. **Which tickets.** The branch names them: every ticket whose `branch` it is; else a `tracker use` choice for this worktree (a shared branch such as `main`); else an id in the branch name; at session start, else the branch's PR. `tracker here` prints the brief again: the handoff first, then the tickets under way in full, the rest in one line. When the match is wrong, `tracker use <id>` puts the ticket on this branch. When the branch holds several tickets and this session's work is one of them, `tracker start <slug> --on <id>` puts this session on it alone (the other sessions keep theirs). Make or switch branches only when the user asks; start a new branch from the branch `context` names (`start:`), else the default branch, name it for the work (led by the ticket's Issue id when it has one), and record it with `tracker set <id> status=in-progress`.
3. **Another ticket or decision.** `tracker context <id>` before you touch it. Ids ignore case and leading zeros; a PR (`#123`), a branch or `<tracker>:<id>` also work. Its dependencies' Carry forward is the contract you build on (`--deep` for the whole chain); its settled decisions hold. `tracker show <ids> --section <name>` prints records' own text, whole or by section (`show D-01 D-02 --section resolution`): read records through it or `context`, not with `cat` or `sed`.
4. **The whole picture**: `tracker index` (`--active`; under its headline, the wait and cycle time lines), `tracker decisions` (the open ones; `--all`), `tracker seq`, `tracker ready` (what can start now: from the default branch, or stacked on a named branch of work under way; answer "what next" from it), `tracker find <text>` (`--all` for every tracker). Read a file only when one of these points you to it.
5. **Changes by others.** A `Changed since your brief` line on a user message reports another session's or GitHub's change to your tickets' dependencies or decisions: act on it.

## Write

Record each fact at the moment it forms, in its home:

| When | Run |
|---|---|
| you start a ticket's work | `tracker context <id>` (a `start:` line names the branch to start from), then `tracker set <id> status=in-progress` (records the branch, and says when it does not contain that base) |
| a step ends, or only the next action changes | `tracker step <id> --next "<one concrete action>"`, with `--carry "<fact>"` for each fact a later ticket must know (a contract, a shared module, a changed rule, a trap). Add a message (`step <id> "<text>" --next ...`) only for what the commit subjects do not say: a result, a measurement, why |
| a ticket ends | `tracker step <id> "..." --done "<what it delivered>"`; `drop` Carry forward to ≤ 5 bullets. Dropped: `tracker set <id> status=dropped summary="<why>"` |
| you stop with the work unfinished: a pause, a compaction, the session's end | `tracker step <id> "..." --pause "<what is done, what is half-done and uncommitted, the next step>"` |
| the agreed plan changes | `tracker add <id> plan "<new>" --replace "<old>"`, or the whole Plan with `tracker put <id> plan -` and a heredoc; and a log line saying why |
| a section changes as a whole (Carry forward kept short, Links sorted) | `tracker put <id> <section> -` with the new text in a heredoc (`<<'EOF'` … `EOF`) |
| you cite a document or a PR | `tracker add <id> link "Label: [title](url) — why"`; README Context: `tracker add tracker context "..."` |
| a run or a measurement supports a ticket or decision | `tracker attach <file> --ref <ids> --note "<what it shows>"` |
| a direction choice is raised or settled | `tracker decide` (below) |
| a ticket must wait, or stops waiting | `tracker wait <id> on\|off <ids>` |
| a note for several tickets, or none | `tracker log "<what changed and why>" --ref <ids>` |
| a `[work-tracker]` line names issue fields due | read each issue with its issue tracker's tool (an MCP server for Shortcut, Jira, Linear …) and `tracker issue <id> --priority "<its word>" --created <ISO 8601 time>`, or `tracker issue <id>` when it has neither. With no such tool, leave them: never guess a value |

- **Text with quotes, backticks or several lines**: pass `-` for any text argument and the text on stdin, in a heredoc with a quoted marker (`<<'EOF'`), so the shell changes nothing. Edit a tracker file by hand only for what `add`, `drop` and `put` do not cover.
- **Subagents** do not write the tracker (a hook tells each one): put what a subagent needs in its prompt (the output of `tracker context <id> --brief`, or the part that matters), and record what it reports, as you record your own work.
- **Each write** prints the `check` problems it adds, and `new`, `decide`, `wait`, `step` and `attach` write their own log line: no `tracker check` or `tracker log` after them.
- **Text limits**: a command refuses a `next`, a `summary`, a log line or a Carry forward bullet over its limit (`tracker rules` lists them). Say it in short; the detail goes in the Plan, the PR or the commits.
- **The id** of `step` and `set` can be left out when the session has one ticket (its `--on` choice, or the one ticket under way on the branch): `tracker step "<what and why>" --next "..."`.
- **Carry forward** holds only what a *later* ticket needs; this ticket's build detail goes in the PR and commits. `tracker drop <id> carry "<text>"` removes a bullet that no longer holds.
- **Decisions** are *direction* choices only (`tracker rules` gives the bar); a choice inside one ticket's build goes in its Plan or `next`. Record each one when it is raised or settled, in chat, in an answered question or in review:
  - raised: `tracker decide "<title>" --refs <ids> --question "<what and why it matters>" [--owner <who>]`; it stops tickets: `--blocks <ids>`
  - new option or fact: `tracker decide D-<n> --note "..."`
  - settled: `tracker decide D-<n> --resolve "<answer>" --by <who>`; settled on the spot: `tracker decide "<title>" --resolve "..." --by <who>`
- **Order and blockers** live in `depends_on` alone; the README may keep *why* an order was chosen. An external blocker: `tracker wait <id> on EXT-12 --link "<url> — <why it blocks>"`.
- **Hook lines** tagged `[work-tracker]` are the tracker's requests: act on each one in the same turn.

Completion: each ticket under way has a `next` true as of now (a `[work-tracker]` line says when commits pass it), every fact above is in its home, and each `check` error a write printed is fixed.

## View

`tracker open [id]` opens the live viewer for the user. Its Now section shows the tickets under way, each branch's handoff and how long ago its commits were last logged. Its Refresh (top right) pulls PR state at once and asks the next prompt for the due issue fields.

## New tracker or migration

- **New**: `tracker init <slug> --title "..." --owner <name> [--repo owner/name[,owner/other]]` (`--repo` enables GitHub sync). Fill `README.md`: Context first (every document linked from the parent issue or brief), then Goal and Scope. Add tickets with `tracker new <ID> --title "..." [--group <label>] [--depends <ids>]`; a group is only a label.
- **Older tracker**: when `tracker check` says so, run `tracker migrate --dry-run`, then `tracker migrate` (it backs up first).
- **A plan from elsewhere**: plan text → Plan; facts later tickets rely on → Carry forward; open questions and blockers → decisions; dated notes → one log line each, with a link to the detail; measurement files → `attach`.
