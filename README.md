# work-tracker

A Claude Code and Codex plugin that keeps the plan and progress of a piece of work (a feature, a project, a migration) in plain Markdown on your machine. The agent reads it at the start of each session and updates it as it works, so the next session continues where the last one stopped.

- Tickets with dependencies, direction decisions and a dated log.
- Actions: tasks for you that the agent cannot or should not do, such as a follow-up with a coworker.
- Hooks log your commits and pull PR state from GitHub.
- A live page in the browser shows the work under way and whose move each ticket waits on.

## Usage

Talk to the agent. It runs the `tracker` CLI for you.

1. **Create a tracker.** Ask the agent, for example: "Make a tracker for the payments rework in acme/api, with tickets for the schema, the API and the admin page." Give it the documents the work answers to (spec, issue, design): they go in the tracker's Context. Name the GitHub repo, so PR state syncs.
2. **Start a session on it.** Say "work on the payments tracker", or type `/work-tracker:tracker start payments` in Claude Code or `$work-tracker:tracker start payments` in Codex. The agent gets a brief: where the work is, what is under way, what it waits on.
   - A new session on a feature branch that a ticket of one tracker names links to that tracker by itself, and says so. To undo, tell the agent it is not that work: it unlinks, and the branch gets no link or offer for a day.
   - Anywhere else on a branch with an open ticket (a default branch such as `main`, a branch whose name only holds a ticket id, a branch of tickets in more than one tracker), the session asks at your first message whether to link it. Answer yes, or "Not now" (no offer on that branch for a day).
   - After `/clear`, the new session stays on the tracker (and the `--on` ticket) of the session it replaces.
3. **Work as usual.** The agent records steps, decisions and pauses as they happen. The hooks log each commit and keep PR state current. You write nothing yourself.
4. **See it.** Ask the agent to open the tracker, or run `tracker open`. The page updates live.
5. **Oversee many sessions (optional).** In a separate session, type `/work-tracker:watch payments` in Claude Code or `$work-tracker:watch payments` in Codex. It notifies you when something needs you: a review came back, checks fail, an agent waits on you. Invoke the same skill with `stop` to end it. In a terminal: `tracker watch payments`.

`/work-tracker:tracker <command>` runs any `tracker` command, for example `/work-tracker:tracker index --active`.

### Useful commands

| Command | Does |
|---|---|
| `tracker list` | all trackers (`--archived`: the archived ones too) |
| `tracker index --active` | the tickets under way, whose move each waits on, the open decisions; under the headline, the wait and cycle time lines |
| `tracker ready` | what can start now, and from which branch |
| `tracker context <id>` | one ticket or decision in full, with what it builds on |
| `tracker decisions --all` | the decisions, open and settled |
| `tracker actions` | your open actions (`--all`: the closed ones too) |
| `tracker find <text>` | search a tracker (`--all`: every tracker) |
| `tracker history` | the log's last lines (`--ref <ids>`, `--since <date>`) |
| `tracker open [id]` | the live page |
| `tracker archive <slug>` | put a tracker away: out of every list, lookup and hook, still in the viewer; `tracker unarchive <slug>` brings it back |
| `tracker delete <slug>` | move a tracker's folder, archived or not, to the system's trash (only in a terminal; refused while a session or a watch is on it) |
| `tracker issue --due` | the tickets whose issue fields (priority, size, when the issue was created) are due, with their issue links and how to record them |
| `tracker check` | problems in the tracker's files |
| `tracker rules` | the full format: keys, statuses, sections, text limits |
| `tracker <command> --help` | the syntax of a command |

Claude's session hook adds `tracker` to its Bash PATH. Codex's session hook gives the agent the installed `bin/tracker` path and the hook's session id as a command prefix. The CLI also reads `CLAUDE_CODE_SESSION_ID` or `CODEX_THREAD_ID` when `TRACKER_SESSION` is absent. In a terminal, run the plugin's `bin/tracker`, or put that folder on your PATH.

### Install in Codex

Add this folder to a [personal or repo marketplace](https://developers.openai.com/plugins/build/plugins), then run `codex plugin add work-tracker@<marketplace-name>`. Codex accepts the existing Claude-compatible manifest. Review and trust the plugin's hooks in Codex, then start a new chat. Both hosts use the same tracker files.

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
| `README.md` | the work: Context, Goal, Scope; optionally Instructions, this work's standing rules for the agent, which every brief prints |
| `tickets/<ID>.md` | one ticket: status, branch, next action, dependencies, priority (0-4), size (1-5), its issue's creation time; Plan, Carry forward (facts later tickets need), Links |
| `decisions/D-<n>.md` | one direction decision: Question, Options, Resolution |
| `actions/A-<n>.md` | one action for you: what to do, the tickets or decisions it concerns, notes |
| `log.md` | dated one-line history |
| `evidence/` | files the records cite: runs, measurements |
| `.state.json` | machine state. Do not edit |

- **Actions.** A task that the agent cannot or should not do (talk to a person, get an access or a sign-off) is an action for you. The agent asks before it adds one (`tracker act "<text>" --refs <ids>`, `--due <date>` when you give a day), and closes it when you say it is done or no longer needed (`tracker act A-<n> --done` or `--drop`). The brief lists the open ones, soonest due first, so the agent adds none twice and asks about one past its due day, or with none, open longer than a week.
- The folder is outside every repo, so all worktrees and sessions share one copy. Writes take a lock, so sessions that run at the same time do not overwrite each other.
- For history, run `git init` in the folder.
- Nothing leaves your machine except the `gh` calls to GitHub. The viewer listens on 127.0.0.1 only.
- **Priority.** A ticket's priority is a number from 0 (most urgent) to 4 (least), the same whatever the issue tracker. A ticket with an `Issue:` link takes its issue's priority: the agent puts that tracker's levels in order onto 0-4 (Highest or Urgent 0, High 1, Medium 2, Low 3, Lowest 4). A ticket without one gets the agent's call when it is added (`tracker new --priority`, `tracker set <id> priority=<n>`).
- **Size.** A ticket's size is a number from 1 (XS) to 5 (XL), the same whatever the issue tracker. A ticket whose issue has an estimate takes it: the agent puts that tracker's scale in order onto 1-5 (Fibonacci points 1 or less, 2, 3, 5, 8 or more; powers of two 1, 2, 4, 8, 16 or more; T-shirt XS to XL). Any other ticket gets the agent's estimate from its Plan (`tracker new --size`, `tracker set <id> size=<n>`).
- **The agent's own values.** A priority or size the agent sets by its own judgement is its best estimate. When it does not know the value or cannot estimate it reliably, it leaves the value empty.
- **Issue fields.** A ticket with an `Issue:` link can carry its issue's priority, estimate (as its size) and creation time. The tracker never calls an issue tracker: the agent reads the issue with its own tool for it (such as an MCP server you have signed in to) and records what it read with `tracker issue`. The brief names the tickets whose fields are due, this session's first: open and never read, or read before the viewer's last Refresh (press it to have open tickets read again); closed, only once and only with a recorded start, for its wait time. `tracker issue --due` gives their links and how to record them. With no such tool the agent leaves them blank.

### Tickets, branches and sessions

- A ticket's status is `todo`, `in-progress`, `done` or `dropped`. Its PR adds `in-review` and `merged`.
- `depends_on` sets the order and the blockers. What is ready or blocked, and the branch a ticket can start from (stacked on work under way), are computed.
- The branch names the session's tickets: each ticket whose `branch` it is, a `tracker use <id>` choice, a ticket id or Issue id in the branch name, or the branch's PR.
- A new session starts on no tracker, unless its branch is sure (a feature branch that a ticket of one tracker names) or it follows a `/clear` on a tracker. Many sessions can share one tracker; `tracker start <slug> --on <id>` puts a session on one ticket of a shared branch.
- The tracker never changes git.
- **Isolation**: the tracker is private to the work. Outside it (code, commits, branch names, PRs, issues) the agent writes each fact in its own words and cites only real ids (an Issue id, a PR number, a URL), never the tracker's ids or name.

### Hooks

Each hook runs `scripts/hook.sh`, which filters the event in shell first. In a session with no tracker a hook exits in about 6 ms; Python (about 40 ms) starts only when it can add something. A hook never blocks the session.

| Event | Does |
|---|---|
| SessionStart | Gives the session its command prefix or shell environment. With a tracker: logs new commits, syncs PR state (at most every 10 min) and injects the brief. With none: after `/clear`, takes the tracker of the session it replaces; on a feature branch that a ticket of one tracker names, links that tracker; on another branch with an open ticket, has the agent offer the link at your first message. |
| UserPromptSubmit | Reports what other sessions or GitHub changed since the brief: a move on its tickets that became yours, a ticket that ended, a dependency's state or Carry forward, a decision. On the first message and every 5th: one state line, with work the tracker may not show (unlogged commits, uncommitted files). Starts a stale GitHub sync in the background. Handles `/work-tracker:watch`. |
| PostToolUse (Bash) | After a commit: logs it, and once per next action asks whether a step ended. After `git push` or `gh pr …`: records the branch on its ticket and syncs PR state. |
| PostToolUse (Edit, Write, MultiEdit, apply_patch) | Counts a hand edit of a tracker file as this session's own. |
| PostToolUse (AskUserQuestion, request_user_input) | Asks the agent to record the answer when it settles a direction decision. |
| SubagentStart | Tells a subagent the tracker and ticket, and that it writes nothing to them. |
| Stop | Logs the commits no other hook saw. |
| SessionEnd | Logs remaining commits and ends the session's activity in the viewer. On `/clear`, leaves the session's tracker for the next session in the same directory. |

### Viewer

- `tracker open [id]` starts a local server (Python `http.server`, 127.0.0.1 only) when none runs, and opens the page. The page polls every 3 s and updates in place.
- The server uses one port, 7316 (`TRACKER_VIEWER_PORT`), so a bookmark or a page left open works again when a server runs: `http://127.0.0.1:7316/` (or `localhost:7316`) lists the trackers. If another program has the port, the server takes a free one.
- A tracked agent session's hooks (session start, each message) start the server when none runs. The server runs while a page polls it or an agent session is on a tracker, and stops about 3 min after neither does. A page that saw the server stop reloads itself when a server answers again.
- **Tracker menu** (top left) lists every tracker. A tracker opens in the same tab. Its ⋯ menu has Archive (Unarchive for an archived tracker) and Delete. The archived trackers sit in a closed section at the bottom. An archived tracker is in `$TRACKER_HOME/.archive/`: no list, lookup, hook or GitHub sync reads it, so it costs nothing. Its page still opens, read-only, with an Unarchive button. A slug names one tracker: `tracker init` refuses the slug of an archived one. Delete moves the tracker's folder to the system's trash (macOS Trash, Windows Recycle Bin, the freedesktop trash on Linux) after you confirm; restore it from there. Archive and Delete are off while an agent session or a watch is on the tracker; end them first. If the trash refuses the folder, the folder stays and the dialog shows why.
- **Your actions**, above Now, lists your open actions, soonest due first, one line each: its title, "Due: <day>" when it has a due day (red once past), and Done and Drop buttons. A row opens to what it concerns, its dates and its notes. A closed action moves to Reference, at the bottom of the page, beside the closed decisions. With none open, the section is not shown.
- **Now** shows the tickets under way (your move first), each branch's handoff, and the agent sessions on this machine that work on the tracker (a ring spins while one works).
- While a page is open, the server syncs PR state every 2 min.
- **Wait time** runs from when a ticket's issue was created (its `issue_created`) to when the ticket started (its `started_at`, which the first `tracker set <id> status=in-progress` records). **Cycle time** runs from that start to when its PR merged (`merged_at`), so it needs no issue tracker. Above the filters, a line per time gives the median, the fastest ticket, and the median of those that ended in the last 7 days; the Time column gives each ticket's, as `wait → cycle`. A ticket without both exact times, with the end before the start (an issue created after the work started), or dropped, has none: a ticket started before 0.29 has no start time, and a merge synced before 0.29 is known only by its date.
- **Refresh** (top right, beside the live line) pulls PR state from GitHub at once and asks the agent, at your next message in a session on the tracker, to read the due issue fields. The live line says when issue fields were last read, or that a Refresh waits for a session, or that no session is open on the tracker.
- **Sequence** lists the tickets in dependency order, depth first as `git log --topo-order` does: each ticket after all it waits on, and the tickets it lets start right after it, so a chain stays together; dropped tickets go last. The Step column draws the dependency graph: a dot per ticket in the column of its step, and a line from each ticket to the tickets that wait on it, left out when a longer chain already links the two. A step's dots and lines share a colour. A line leaves its dot level, runs up or down in the gap left of the waiting ticket's step, and enters that dot level, so it passes through no other dot and every line runs right. A closed ticket's dot is hollow, and a dropped one's is grey. The graph is drawn again for the rows as shown, in any sort and filter; a link to a hidden row is left out. Hover or focus a row to see in the graph only what its ticket waits on and unblocks, through any chain, with the rest grey: each of those lines is green when the ticket waited on is closed, amber when it is under way, and red when it still blocks. The Priority column shows each ticket's priority as P0 (most urgent) to P4 and sorts most urgent first. The Size column shows each ticket's size as XS to XL and sorts smallest first. The Deps column lists what a ticket waits on (`←`) on one line and what it unblocks (`→`) on the next. A column heading sorts by that column and a second press reverses it; Time and Deps have a heading for each of their two values, and Waits on and Unblocks sort by count, tickets with no value in the column (no group, priority, size, wait or cycle time) go last, and ties keep the dependency order. Step puts the dependency order back. A sequence narrower than 920 px (a window of about 1000 px) leaves out Group and Time, and one narrower than 620 px also Priority, Size and Deps; the opened ticket shows its group and its start and merge times. The sort is kept in the address (`?sort=group`, `?sort=-group`), so a reload or a shared link keeps it.
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
| `TRACKER` | none | a tracker slug: commands use it first, the hooks when `tracker start` chose none |
| `TRACKER_NUDGE_EVERY` | 5 | messages between state lines |
| `TRACKER_VIEWER_IDLE` | 180 | seconds before an unused viewer stops |
| `TRACKER_VIEWER_PORT` | 7316 | the viewer's port; 0: a free port each start, and the hooks start no viewer |
| `TRACKER_VIEWER_SYNC` | 120 | seconds between the viewer's GitHub syncs |
| `TRACKER_WATCH_SYNC` | 120 | seconds between the watcher's GitHub syncs |
| `TRACKER_WATCH_IDLE` | 600 | seconds before an idle agent that waits on you is reported |
| `TRACKER_WATCH_BUSY` | 2700 | seconds a busy agent can record nothing before it is reported |

## Develop

- Run from a checkout in Claude Code: `claude --plugin-dir <path to this folder>`, then `/reload-plugins` after an edit. In Codex, install through the marketplace. Codex caches an installed copy by version: to try a change, raise the manifest version in your checkout, run `codex plugin add work-tracker@<marketplace-name>` again, and put the version back before you commit. An open viewer restarts itself when `scripts/tracker/` changes, and an open page reloads when `viewer/` changes.
- Test: `python3 -m unittest discover tests` (about 10 s, no network). Lint: `uvx ruff check`.
- Changelog: a commit with a change that users see adds a line per change under `## Unreleased` in `CHANGELOG.md`, below a `### Added`, `### Changed`, `### Fixed` or `### Removed` heading.
- Release: `scripts/release.py patch|minor|major` on `main`, `--dry-run` to see the notes first. It runs the tests and lint, moves the Unreleased lines to a `CHANGELOG.md` section for the version, bumps `version` in `.claude-plugin/plugin.json` (installed copies are cached by version), commits, tags `v<version>`, pushes both and makes the GitHub release.
- The format's contract (keys and who writes each, statuses, link labels, sections) is the constants in `scripts/tracker/model.py`. `tracker rules` prints it, `tracker check` enforces it, and the hooks and templates read it.
- Each fact has one home, and every view is computed from it. `skills/tracker/reference.md` lists each home.

### Code

`scripts/tracker/`: stdlib only, run by `bin/tracker` and `scripts/hook.sh`. One module per concern; each imports only the modules before it:

1. `markdown`: the file format as text (frontmatter, sections, link lines)
2. `model`: contract constants, records, dependencies, body edits, the lock
3. `git`
4. `session`: the session's tracker, branch matching, commit marks and handoffs, the sessions running now
5. `contract`: `check`, `rules`, `migrate`
6. `views`: text views and the brief
7. `github`: `sync`
8. `watcher`: `tracker watch` and the user's grant; archiving and deleting a tracker
9. `viewer`: the page and its server (look: `viewer/page.html`, `style.css`, `app.js`). Markup is `Html`, which escapes the text put into it; the sequence's columns are its `Column` table
10. `hooks`
11. `cli`

### Evals

`evals/` holds [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals) cases. They check that the skill, the brief and the hooks lead the model to record work through the CLI and to answer from the tracker's views; each case's `prompt.md` says what it checks. Each run is a full Claude session, billed to your plan: run them only after a significant change to those texts, not as a routine check. Run them with `evals/run.sh` (its header explains the fixed options):

```
evals/run.sh quick <tag>...   # the cases with any of the tags, once each, on Sonnet
evals/run.sh quick all        # every case, once each, on Sonnet
evals/run.sh release          # every case, its own run count, on Opus: before a release
```

- Tags name the area a case tests. Pick the tags of the text you changed:
  - `write`: the write commands and their output, the skill's Write table
  - `log`: what is logged: the commit hooks, `step` messages
  - `decision`: `decide`, the decision bar, the question hook
  - `issue`: issue fields
  - `read`: `index`, `ready`, `context` and what can start
  - the skill's other parts and the brief's protocol reach every case: `quick all`
- A case runs 3 times in `release`, or once when its `prompt.md` sets `runs: 1` (one plain command that passed in every full run).
- Options after the mode's words go to `claude plugin eval` as they are, such as `--case <name>` or `--keep-temp`.
- Each case's `scaffold.sh` sources `evals/lib/demo-tracker.sh`, which makes a git repo and a `demo` tracker in the run's workspace. The sandbox lets the agent write only there, so the tracker is in `.trackers/`, linked from `~/.claude/trackers`.
- On macOS with only the Xcode `git` (`/usr/bin/git`), git cannot run in the sandbox, so the cases leave git to the scaffold and the hooks.
