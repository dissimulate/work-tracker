---
name: tracker
description: Work tracker - the local plan and progress record for a piece of work (tickets, decisions, log). Use when the user says to start or work on a tracker or epic by name, when a `[work-tracker]` block is in context, before starting, after finishing or before pausing work on a tracked ticket, when a decision is made or opened, when the user has a task only they should do, or when the user asks to create, view, migrate or update a tracker.
argument-hint: "[tracker command, e.g. index --active]"
---

# Work tracker

A tracker is the single record of a piece of work's plan and state: the **hand-off** to the next session, which starts with no memory of this one, and the page the user watches (`tracker open`). It lives in `$TRACKER_HOME/<slug>/` (default `~/.claude/trackers/<slug>/`), outside every repo, so every worktree and session reads one copy. It is **isolated**: the isolation rule, first in the brief's protocol and in `tracker rules`, governs every text you write outside it.

The CLI is `tracker`; `tracker <command> --help` gives each command's syntax. If this skill is invoked with arguments, run `tracker $ARGUMENTS` and report the result; with none, run `tracker start`.

Use the command prefix from the session's `[work-tracker]` hook for every call when supplied. If `tracker` is absent from PATH, use the absolute path to `bin/tracker` at this plugin's root (two directories above this file).

## Model

A tracker holds `README.md` (the work: Context, Goal, Scope, optional Instructions), `tickets/<ID>.md`, `decisions/D-<n>.md`, `actions/A-<n>.md`, `log.md` and `evidence/`. **One source per fact**: each fact has one home and every view is computed from it, so write it once, through the commands below. Computed, never written: ready, blocked and the branch a ticket starts from (from `depends_on`), PR state and whose move (from `sync`; `next` holds only your own next action), wait and cycle time (quote `tracker index`), and the commits (the hooks log each one on the branch of a ticket under way).

Read [reference.md](reference.md) (next to this file) before you create a tracker, migrate one, import a plan from elsewhere or edit a tracker file by hand, and when you are unsure where a fact goes: it maps every fact to its home. `tracker rules` is the full contract (every key and what writes it, the statuses, labels, sections and the decision bar); read it when a command refuses a key or a label. `tracker check` enforces it.

## Read

1. **Which work.** `tracker start <name words>` ties the tracker the user names to this session (its hooks and commands then use it) and prints the brief; `tracker start` alone takes the tracker with an open ticket on this branch. When it lists options (exit 3), ask the user which one. A new session links by itself on a feature branch that a ticket of one tracker names, and after `/clear` keeps the tracker of the session it replaces; the brief says so. If the user says this session is not that work, run `tracker start --decline`. Elsewhere it starts on no tracker; on a branch with an open ticket, a `[work-tracker]` line at its start asks you to offer the link. Link only on the user's yes; on "Not now", run `tracker start --decline` (no offer on this branch for a day). `--tracker <slug>` works for one command.
2. **Which tickets.** The branch names them: every ticket whose `branch` it is; else a `tracker use` choice for this worktree (a shared branch such as `main`); else an id in the branch name; at session start, else the branch's PR. `tracker here` prints the brief again: the handoff first, then the tickets under way in full, the rest in one line. When the match is wrong, `tracker use <id>` puts the ticket on this branch. When the branch holds several tickets and this session's work is one of them, `tracker start <slug> --on <id>` puts this session on it alone (the other sessions keep theirs). Make or switch branches only when the user asks; start a new branch from the branch `context` names (`start:`), else the default branch, name it for the work (led by the ticket's Issue id when it has one), and record it with `tracker set <id> status=in-progress`.
3. **Another ticket or decision.** `tracker context <id>` before you touch it. Ids ignore case and leading zeros; a PR (`#123`), a branch or `<tracker>:<id>` also work. Its dependencies' Carry forward is the contract you build on (`--deep` for the whole chain); its settled decisions hold. When a README Context document governs a choice you are making, open it and work from it. `tracker show <ids> --section <name>` prints records' own text, whole or by section (`show D-01 D-02 --section resolution`): read records through it or `context`, not with `cat` or `sed`.
4. **The whole picture**: `tracker index` (`--active`; under its headline, the wait and cycle time lines), `tracker decisions` (the open ones; `--all`), `tracker actions` (the user's open tasks; `--all`), `tracker seq`, `tracker ready` (what can start now: from the default branch, or stacked on a named branch of work under way; answer "what next" from it), `tracker find <text>` (`--all` for every tracker), `tracker history` (the log's last lines; `--ref <ids>`, `--since <date>`). `tracker open [id]` opens the live page for the user. Read a file only when one of these points you to it.
5. **Changes by others.** A `Changed since your brief` line on a user message reports another session's or GitHub's change to your tickets' dependencies or decisions: act on it.

## Write

Record each fact at the moment it forms, in its home:

| When | Run |
|---|---|
| you start a ticket's work | `tracker context <id>` (a `start:` line names the branch to start from), then `tracker set <id> status=in-progress` (records the branch, and says when it does not contain that base) |
| a step ends, or only the next action changes | `tracker step <id> --next "<one concrete action>"`, with `--carry "<fact>"` for each fact a later ticket must know (a contract, a shared module, a changed rule, a trap). Add a message (`step <id> "<text>" --next ...`) only for what the commit subjects do not say: a result, a measurement, why. Log no push, merge, review round or test run: the PR, its checks and `sync` hold those |
| a ticket ends | `tracker step <id> --done "<what it delivered>"`; `drop` Carry forward to ≤ 5 bullets. Dropped: `tracker set <id> status=dropped summary="<why>"` |
| you stop with the work unfinished: a pause, a compaction, the session's end | `tracker step <id> "..." --pause "<what is done, what is half-done and uncommitted, the next step>"` |
| the agreed plan changes | `tracker add <id> plan "<new>" --replace "<old>" --why "<why>"`, or the whole Plan with `tracker put <id> plan - --why "<why>"` and a heredoc |
| a section changes as a whole (Carry forward kept short, Links sorted), or the README needs a section of its own (`put tracker "Why this order" -` makes it) | `tracker put <id> <section> -` with the new text in a heredoc (`<<'EOF'` … `EOF`) |
| you cite a document or a PR | `tracker add <id> link "Label: [title](url) — why"`; README Context: `tracker add tracker context "..."` |
| a run or a measurement supports a ticket or decision | `tracker attach <file> --ref <ids> --note "<what it shows>"`; more text for a file it keeps: `tracker attach <name> --append -` with a heredoc |
| a direction choice is raised or settled | `tracker decide` (below) |
| the user gives a standing rule for this work (how to handle a kind of ticket, an action to add when something happens) | `tracker put tracker instructions -` with the whole section in a heredoc, or one line `tracker add tracker instructions "..."`; follow the section the brief prints |
| a task only the user should do: talk to or follow up with a person, get an access or a sign-off, a step on a system you cannot reach | ask the user whether to add it; on yes, `tracker act "<what to do, with whom>" --refs <ids>`, with `--due YYYY-MM-DD` only when the user gives a day; it stops tickets: `--blocks <ids>`. When the user says it is done or no longer needed: `tracker act A-<n> --done` (or `--drop`), with `--note "<outcome>"` when it has one. A new text: `tracker act A-<n> --title "..."` |
| a ticket must wait, or stops waiting | `tracker wait <id> on\|off <ids>`; on an external blocker: `tracker wait <id> on EXT-12 --link "<url> — <why it blocks>"` |
| a note for several tickets, or none | `tracker log "<what changed and why>" --ref <ids>` |
| a `[work-tracker]` line names issue fields to read | with a tool for their issue tracker (such as an MCP server), run `tracker issue` and record what it asks for; with none, leave them: never guess a value |

- **Text with quotes, backticks or several lines**: pass `-` for any text argument and the text on stdin, in a heredoc with a quoted marker (`<<'EOF'`), so the shell changes nothing. Edit a tracker file by hand only for what `add`, `drop` and `put` do not cover.
- **Subagents** do not write the tracker: put what one needs in its prompt (`tracker context <id> --brief`, or the part that matters), and record what it reports as your own work.
- **Each write** logs what the history needs and ends with what `check` found new: run no `tracker check` or `tracker log` after it.
- **The id** of `step` and `set` can be left out when the session has one ticket (its `--on` choice, or the one ticket under way on the branch): `tracker step "<what and why>" --next "..."`.
- **Carry forward** holds only what a *later* ticket needs; this ticket's build detail goes in the PR and commits. `tracker drop <id> carry "<text>"` removes a bullet that no longer holds.
- **Decisions** are *direction* choices only (`tracker rules` gives the bar); a choice inside one ticket's build goes in its Plan or `next`. Record each one when it is raised or settled, in chat, in an answered question or in review:
  - raised: `tracker decide "<title>" --refs <ids> --question "<what and why it matters>" [--owner <who>]`; it stops tickets: `--blocks <ids>`
  - new option or fact: `tracker decide D-<n> --note "..."`
  - settled: `tracker decide D-<n> --resolve "<answer>" --by <who>`; settled on the spot: `tracker decide "<title>" --resolve "..." --by <who>`
- **Actions** are few and only the user's: never your own next step (`next`), a wait the PR shows (a review, checks) or a choice (a decision). Before you add one, check the open ones in the brief or `tracker actions`; add to one with `tracker act A-<n> --note "..."`. A note is one fact: the context when you add the action, the reply when it comes, each its own `--note` (it repeats). `tracker show A-<n>` prints an action with its notes. One open longer than a week: ask the user whether it is done.
- **Hook lines** tagged `[work-tracker]` are the tracker's requests: act on each one in the same turn.

Completion: each ticket under way has a `next` true as of now (a `[work-tracker]` line says when commits pass it), every fact above is in its home, and each `check` error a write printed is fixed.
