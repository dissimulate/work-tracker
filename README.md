# work-tracker

A Claude Code and Codex plugin that keeps the plan and progress of a piece of work (a feature, a project, a migration) in plain Markdown on your machine. The agent reads it at the start of each session and updates it as it works, so the next session continues where the last one stopped.

- Tickets with dependencies, direction decisions and a dated log.
- Hooks log your commits and pull PR state from GitHub.
- A live page in the browser shows the work under way and whose move each ticket waits on.

## Usage

Talk to the agent. It runs the `tracker` CLI for you.

1. **Create a tracker.** Ask the agent, for example: "Make a tracker for the payments rework in acme/api, with tickets for the schema, the API and the admin page." Give it the documents the work answers to (spec, issue, design): they go in the tracker's Context. Name the GitHub repo, so PR state syncs.
2. **Start a session on it.** Say "work on the payments tracker", or type `/work-tracker:tracker start payments` in Claude Code or `$work-tracker:tracker start payments` in Codex. The agent gets a brief: where the work is, what is under way, what it waits on.
   - A new session on a branch with an open ticket asks at your first message whether to link it. Answer yes, or "Not now" (no offer on that branch for a day).
3. **Work as usual.** The agent records steps, decisions and pauses as they happen. The hooks log each commit and keep PR state current. You write nothing yourself.
4. **See it.** Ask the agent to open the tracker, or run `tracker open`. The page updates live.
5. **Oversee many sessions (optional).** In a separate session, type `/work-tracker:watch payments` in Claude Code or `$work-tracker:watch payments` in Codex. It notifies you when something needs you: a review came back, checks fail, an agent waits on you. Invoke the same skill with `stop` to end it. In a terminal: `tracker watch payments`.

`/work-tracker:tracker <command>` runs any `tracker` command, for example `/work-tracker:tracker index --active`.

### Useful commands

| Command | Does |
|---|---|
| `tracker list` | all trackers |
| `tracker index --active` | the tickets under way, whose move each waits on, the open decisions; under the headline, the wait and cycle time lines |
| `tracker ready` | what can start now, and from which branch |
| `tracker context <id>` | one ticket or decision in full, with what it builds on |
| `tracker decisions --all` | the decisions, open and settled |
| `tracker find <text>` | search a tracker (`--all`: every tracker) |
| `tracker open [id]` | the live page |
| `tracker issue --due` | the tickets whose issue fields (priority, when the issue was created) are due, with their issue links |
| `tracker check` | problems in the tracker's files |
| `tracker rules` | the full format: keys, statuses, sections, text limits |
| `tracker <command> --help` | the syntax of a command |

Claude's session hook adds `tracker` to its Bash PATH. Codex's session hook gives the agent the installed `bin/tracker` path and the hook's session id as a command prefix. The CLI also reads `CLAUDE_CODE_SESSION_ID` or `CODEX_THREAD_ID` when `TRACKER_SESSION` is absent. In a terminal, run the plugin's `bin/tracker`, or put that folder on your PATH.

### Install in Codex

Add this folder to a [personal or repo marketplace](https://developers.openai.com/plugins/build/plugins), then run `codex plugin add work-tracker@<marketplace-name>`. Codex accepts the existing Claude-compatible manifest. Review and trust the plugin's hooks in Codex, then start a new chat. Both hosts use the same tracker files; `TRACKER_HOME` changes their root when needed.

## Requirements

- macOS, Linux or Windows. On Windows, Git for Windows: the hooks and the CLI are `sh` scripts, and Claude Code runs them in its Git Bash.
- Python 3.9 or later, standard library only: `python3`, or on Windows `python` or `py`. The macOS system Python works.
- `git`.
- Optional: the GitHub CLI `gh`, logged in. Without it there is no PR state (in review, merged, reviews, checks) and no "whose move".

## How it works

### Data

Each tracker is a folder in `~/.claude/trackers/<slug>/` (`TRACKER_HOME` changes the root):

| File | Holds |
|---|---|
| `README.md` | the work: Context, Goal, Scope |
| `tickets/<ID>.md` | one ticket: status, branch, next action, dependencies, its issue's priority and creation time; Plan, Carry forward (facts later tickets need), Links |
| `decisions/D-<n>.md` | one direction decision: Question, Options, Resolution |
| `log.md` | dated one-line history |
| `evidence/` | files the records cite: runs, measurements |
| `.state.json` | machine state. Do not edit |

- The folder is outside every repo, so all worktrees and sessions share one copy. Writes take a lock, so sessions that run at the same time do not overwrite each other.
- For history, run `git init` in the folder.
- Nothing leaves your machine except the `gh` calls to GitHub. The viewer listens on 127.0.0.1 only.
- **Issue fields.** A ticket with an `Issue:` link can carry its issue's priority and creation time. The tracker never calls an issue tracker: the agent reads the issue with its own tool for it (an MCP server you have signed in to, such as Shortcut's, Jira's or Linear's) and records what it read with `tracker issue`. The brief names the tickets whose fields are due: never read, or open and read more than a day ago or before the viewer's last Refresh. With no such tool the agent leaves them blank.

### Tickets, branches and sessions

- A ticket's status is `todo`, `in-progress`, `done` or `dropped`. Its PR adds `in-review` and `merged`.
- `depends_on` sets the order and the blockers. What is ready or blocked, and the branch a ticket can start from (stacked on work under way), are computed.
- The branch names the session's tickets: each ticket whose `branch` it is, a `tracker use <id>` choice, a ticket id or Issue id in the branch name, or the branch's PR.
- Each new session (after `/clear` too) starts on no tracker. Many sessions can share one tracker; `tracker start <slug> --on <id>` puts a session on one ticket of a shared branch.
- The tracker never changes git.
- **Isolation**: the tracker is private to the work. Outside it (code, commits, branch names, PRs, issues) the agent writes each fact in its own words and cites only real ids (an Issue id, a PR number, a URL), never the tracker's ids or name.

### Hooks

Each hook runs `scripts/hook.sh`, which filters the event in shell first. In a session with no tracker a hook exits in about 6 ms; Python (about 40 ms) starts only when it can add something. A hook never blocks the session.

| Event | Does |
|---|---|
| SessionStart | Gives the session its command prefix or shell environment. With a tracker: logs new commits, syncs PR state (at most every 10 min) and injects the brief. With none, on a branch with an open ticket: has the agent offer the link at your first message. |
| UserPromptSubmit | Reports what other sessions or GitHub changed since the brief. On the first message and every 5th: one state line, with work the tracker may not show (unlogged commits, uncommitted files). Starts a stale GitHub sync in the background. Handles `/work-tracker:watch`. |
| PostToolUse (Bash) | After a commit: logs it, and once per next action asks whether a step ended. After `git push` or `gh pr …`: records the branch on its ticket and syncs PR state. |
| PostToolUse (Edit, Write, MultiEdit, apply_patch) | Counts a hand edit of a tracker file as this session's own. |
| PostToolUse (AskUserQuestion, request_user_input) | Asks the agent to record the answer when it settles a direction decision. |
| SubagentStart | Tells a subagent the tracker and ticket, and that it writes nothing to them. |
| Stop | Logs the commits no other hook saw. |
| SessionEnd | Logs remaining commits and ends the session's activity in the viewer. |

### Viewer

- `tracker open [id]` starts a local server (Python `http.server`, 127.0.0.1 only) when none runs, and opens the page. The page polls every 3 s and updates in place. The server stops about 3 min after the last request.
- **Now** shows the tickets under way (your move first), each branch's handoff, and the agent sessions on this machine that work on the tracker (a ring spins while one works).
- While a page is open, the server syncs PR state every 2 min.
- **Wait time** runs from when a ticket's issue was created (its `issue_created`) to when the ticket started (its `started_at`, which the first `tracker set <id> status=in-progress` records). **Cycle time** runs from that start to when its PR merged (`merged_at`), so it needs no issue tracker. Above the filters, a line per time gives the median, the fastest ticket, and the median of those that ended in the last 7 days; the Wait time and Cycle time columns give each ticket's. A ticket without both exact times, with the end before the start (an issue created after the work started), or dropped, has none: a ticket started before 0.29 has no start time, and a merge synced before 0.29 is known only by its date.
- **Refresh** (top right, beside the live line) pulls PR state from GitHub at once and asks the agent, at your next message in a session on the tracker, to read the due issue fields. The live line says when issue fields were last read, or that a Refresh waits for a session, or that no session is open on the tracker.
- **Sequence** lists the tickets in dependency order. The Priority column shows each ticket's issue priority and sorts most urgent first (Urgent, Highest and P0 first; an unknown word after the known ones). A column heading sorts by that column and a second press reverses it; Waits on and Unblocks sort by count, tickets with no value in the column (no group, priority, wait or cycle time) go last, and ties keep the dependency order. Step puts the dependency order back. The sort is kept in the address (`?sort=group`, `?sort=-group`), so a reload or a shared link keeps it.
- The session list uses hook activity: a user prompt marks a tracked session busy, Stop marks it idle, and SessionEnd marks it ended. Activity expires after a day without events. This shows the last reported state; an interrupted turn can remain busy until the next event. Claude Code's `~/.claude/sessions/*.json` (`CLAUDE_CONFIG_DIR` when set) supplies process liveness and names when available. Codex needs no session-file parser.

### Watch

- `tracker watch [name]` prints each change as one line; `!` marks what needs you. One watcher per tracker.
- Only the user starts it: in a terminal, or with the host's watch skill command, which makes that session a read-only overseer that sends notifications when the host supports them, or replies in chat.
- The CLI refuses `watch` in a session without that grant, and refuses every write in a session with it.

### Upgrading a tracker

The `schema` key in a tracker's README names its format. When `tracker check` says the format is old, run `tracker migrate --dry-run`, then `tracker migrate` (it backs up to `$TRACKER_HOME/.backups/` first).

### Settings

Environment variables. All are optional.

| Variable | Default | Does |
|---|---|---|
| `TRACKER_HOME` | `~/.claude/trackers` | where the trackers are |
| `TRACKER` | none | the tracker slug a session and its commands use when `tracker start` chose none |
| `TRACKER_NUDGE_EVERY` | 5 | messages between state lines |
| `TRACKER_VIEWER_IDLE` | 180 | seconds before an unused viewer stops |
| `TRACKER_VIEWER_SYNC` | 120 | seconds between the viewer's GitHub syncs |
| `TRACKER_WATCH_SYNC` | 120 | seconds between the watcher's GitHub syncs |
| `TRACKER_WATCH_IDLE` | 600 | seconds before an idle agent that waits on you is reported |
| `TRACKER_WATCH_BUSY` | 2700 | seconds a busy agent can record nothing before it is reported |

## Develop

- Run from a checkout in Claude Code: `claude --plugin-dir <path to this folder>`, then `/reload-plugins` after an edit. In Codex, install through the marketplace; after a change, bump the manifest version and run `codex plugin add work-tracker@<marketplace-name>` again. An open viewer restarts itself when `scripts/tracker/` changes, and an open page reloads when `viewer/` changes.
- Test: `python3 -m unittest discover tests` (about 10 s, no network). Lint: `uvx ruff check`.
- Ship: bump `version` in `.claude-plugin/plugin.json`. Installed copies are cached by version.
- The format's contract (keys and who writes each, statuses, link labels, sections) is the constants in `scripts/tracker/model.py`. `tracker rules` prints it, `tracker check` enforces it, and the hooks and templates read it.
- Each fact has one home, and every view is computed from it. `skills/tracker/SKILL.md` lists each home.

### Code

`scripts/tracker/`: stdlib only, run by `bin/tracker` and `scripts/hook.sh`. One module per concern; each imports only the modules before it:

1. `markdown`: the file format as text (frontmatter, sections, link lines)
2. `model`: contract constants, records, dependencies, body edits, the lock
3. `git`
4. `session`: the session's tracker, branch matching, commit marks and handoffs, the sessions running now
5. `contract`: `check`, `rules`, `migrate`
6. `views`: text views and the brief
7. `github`: `sync`
8. `watcher`: `tracker watch` and the user's grant
9. `viewer`: the page and its server (look: `viewer/page.html`, `style.css`, `app.js`)
10. `hooks`
11. `cli`

### Evals

`evals/` holds [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals) cases. They check that the skill, the brief and the hooks lead the model to record work through the CLI (a step with `step --next`, a hook-logged commit not logged again, one decision for a settled direction and none for an approval, a section replaced with `put`, not by hand), and to answer what can start next with the branch it stacks on. They are real model runs, billed to your plan: run them after a change to those texts, not as a routine check.

```
claude plugin eval . --scaffold --allow-tools Bash --ablation none
```

- `--scaffold`: each case's `scaffold.sh` sources `evals/lib/demo-tracker.sh`, which makes a git repo and a `demo` tracker in the run's workspace. The sandbox lets the agent write only there, so the tracker is in `.trackers/`, linked from `~/.claude/trackers`.
- `--ablation none`: with no plugin there is no tracker, so a baseline measures nothing.
- `--runs 1` while you change a case; the default is 3 runs per case.
- On macOS with only the Xcode `git` (`/usr/bin/git`), git cannot run in the sandbox, so the cases leave git to the scaffold and the hooks.
