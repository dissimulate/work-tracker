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
| `tracker issue` | the tickets whose issue fields (priority, size, due day, when the issue was created) are to read, with their issue links and how to record them |
| `tracker check` | problems in the tracker's files |
| `tracker rules` | the full format: keys, statuses, sections, text limits |
| `tracker <command> --help` | the syntax of a command |

In an agent session the hooks give the agent `tracker`. In a terminal, run the plugin's `bin/tracker`, or put that folder on your PATH.

### Install in Codex

Add this folder to a [personal or repo marketplace](https://developers.openai.com/plugins/build/plugins), then run `codex plugin add work-tracker@<marketplace-name>`. Codex accepts the existing Claude-compatible manifest. Review and trust the plugin's hooks in Codex, then start a new chat. Both hosts use the same tracker files.

## Requirements

- macOS, Linux or Windows. On Windows, Git for Windows: the hooks and the CLI are `sh` scripts, and Claude Code runs them in its Git Bash.
- Python 3.9 or later, standard library only: `python3`, or on Windows `python` or `py`. The macOS system Python works.
- `git`.
- Optional: the GitHub CLI `gh`, logged in. Without it there is no PR state (in review, merged, reviews, checks) and no "whose move".

## How it works

### Data

Each tracker is a folder in `~/.claude/trackers/<slug>/` (`TRACKER_HOME` changes the root). The folder is outside every repo, so all worktrees and sessions share one copy. Writes take a lock, so sessions that run at the same time do not overwrite each other. For history, run `git init` in the folder.

| File | Holds |
|---|---|
| `README.md` | the work: Context, Goal, Scope; optionally Instructions, this work's standing rules for the agent, which every brief prints |
| `tickets/<ID>.md` | one ticket: status, branch, next action, dependencies, priority, size, due day, its times; Plan, Carry forward (facts later tickets need), Links |
| `decisions/D-<n>.md` | one direction decision: Question, Options, Resolution |
| `actions/A-<n>.md` | one action for you: what to do, the tickets or decisions it concerns, notes |
| `log.md` | dated one-line history |
| `evidence/` | files the records cite: runs, measurements |
| `.state.json` | machine state. Do not edit |

`tracker rules` prints the full format: every key and what writes it, and each rule below in full.

- **Actions** are tasks for you that the agent cannot or should not do: talk to a person, get an access or a sign-off. The agent asks before it adds one, and closes it when you say it is done or no longer needed. An action can have a due day, and can block tickets as an open decision does. The agent asks you about one past its due day, or with none, open longer than a week.
- **Priority** is a number from 0 (most urgent) to 4 (least), shown as P0 to P4. **Size** is a number from 1 (XS) to 5 (XL), as an amount of work: XS an hour or two, S about a day, M a few days, L about a week, XL more. The scales are the same for every issue tracker, so values compare, sort and add up across them.
- **Issue fields.** A ticket with an `Issue:` link takes its issue's priority, estimate (as its size), due day and creation time. The tracker never calls an issue tracker: the agent reads the issue with its own tool for it (such as an MCP server you have signed in to), maps each value by what it means on that issue tracker's scale (High is 1; 5 Fibonacci points are L), and records it with `tracker issue`. With no such tool, the fields stay empty.
- **The agent's own values.** On a ticket with no issue value, the agent sets priority and size as its best estimate, or leaves them empty when it cannot estimate them reliably. A due day (`YYYY-MM-DD`, on any ticket, decision or action) is only a day that you or the issue tracker gave.
- **Times**: a key that ends `_at` holds a time in UTC to the second, such as `started_at` or `merged_at`. The views show each as its day.
- Nothing leaves your machine except the `gh` calls to GitHub. The viewer listens on 127.0.0.1 only.

### Tickets, branches and sessions

- A ticket's status is `todo`, `in-progress`, `done` or `dropped`. Its PR adds `in-review` and `merged`.
- `depends_on` sets the order and the blockers. What is ready or blocked, and the branch a ticket can start from (stacked on work under way), are computed.
- The branch names the session's tickets: each ticket whose `branch` it is, a `tracker use <id>` choice, a ticket id or Issue id in the branch name, or the branch's PR.
- Many sessions can share one tracker. `tracker start <slug> --on <id>` puts a session on one ticket of a shared branch.
- The tracker never changes git.
- **Isolation**: the tracker is private to the work. Outside it (code, commits, branch names, PRs, issues) the agent writes each fact in its own words and cites only real ids (an Issue id, a PR number, a URL), never the tracker's ids or name.

### Hooks

Each hook runs `scripts/hook.sh`, which filters the event in shell first. In a session with no tracker a hook exits in about 6 ms; Python (about 40 ms) starts only when it can add something. A hook never blocks the session.

| Event | Does |
|---|---|
| SessionStart | Gives the session its command prefix or shell environment. With a tracker: logs new commits, syncs PR state (at most every 10 min) and injects the brief. With none: links the session, or has the agent offer the link, as [Usage](#usage) step 2 says. |
| UserPromptSubmit | Reports what other sessions or GitHub changed since the brief: a move on its tickets that became yours, a ticket that ended, a dependency's state or Carry forward, a decision. On the first message and every 5th: one state line, with work the tracker may not show (unlogged commits, uncommitted files). Starts a stale GitHub sync in the background. Handles `/work-tracker:watch`. |
| PostToolUse (Bash) | After a commit: logs it, and once per next action asks whether a step ended. After `git push` or `gh pr …`: records the branch on its ticket and syncs PR state. |
| PostToolUse (Edit, Write, MultiEdit, apply_patch) | Counts a hand edit of a tracker file as this session's own. |
| PostToolUse (AskUserQuestion, request_user_input) | Asks the agent to record the answer when it settles a direction decision. |
| SubagentStart | Tells a subagent the tracker and ticket, and that it writes nothing to them. |
| Stop | Logs the commits no other hook saw. |
| SessionEnd | Logs remaining commits and ends the session's activity in the viewer. On `/clear`, leaves the session's tracker for the next session in the same directory. |

### Viewer

`tracker open [id]` opens the live page. It polls every 3 s and updates in place.

- **Server**: Python's `http.server` on 127.0.0.1, port 7316 (`TRACKER_VIEWER_PORT`; a free port when another program has it), so a bookmark or a page left open works again when a server runs: `http://127.0.0.1:7316/` lists the trackers. A tracked agent session's hooks start it. It stops about 3 min after no page polls it and no agent session is on a tracker. A page that saw it stop reloads itself when a server answers again. While a page is open, the server syncs PR state every 2 min.
- **Tracker menu** (top left) lists every tracker, the archived ones in a closed section at the bottom. A tracker's ⋯ menu has Archive (Unarchive) and Delete, both off while an agent session or a watch is on the tracker.
  - Archive moves the tracker to `$TRACKER_HOME/.archive/`: no list, lookup, hook or GitHub sync reads it. Its page still opens, read-only. `tracker init` refuses its slug.
  - Delete moves the tracker's folder to the system's trash after you confirm; restore it from there. If the trash refuses the folder, the folder stays and the dialog shows why.
- **Refresh** (top right) pulls PR state from GitHub at once, and has the agent read the issue fields again at your next message in a session on the tracker. The live line beside it says when they were last read.
- **Your actions**: your open actions, soonest due first, with Done and Drop. A due day is red once past. A row opens to what the action blocks and touches, its dates and its notes.
- **Now**: the tickets under way (your move first), each branch's handoff, and the agent sessions on the tracker (a ring spins while one works). A session is busy from your message to its turn's end; an interrupted turn can show busy until the next event.
- **Sequence**: the tickets in dependency order, depth first so a chain stays together, dropped tickets last. The Step column draws the dependency graph:
  - a dot per ticket: blue when ready, amber when under way or it can stack on work under way, red when blocked, a green ring when closed, a grey ring when dropped. The ticket's id takes the same colour.
  - a line from each ticket to each ticket that waits on it: red while it blocks, amber while under way, green once closed.
  - Hover or focus a row to see only what its ticket waits on and unblocks.
  - A column heading sorts by that column, and a second press reverses it. Step puts the dependency order back. The address keeps the sort (`?sort=-group`). A narrow window leaves out Group, then Priority, Size and Deps.
- **Wait time** runs from when a ticket's issue was created (`issue_created_at`) to when the ticket started (`started_at`, which its first `tracker set <id> status=in-progress` records). **Cycle time** runs from that start to when its PR merged (`merged_at`). Above the filters, a line per time gives the median, the fastest ticket, and the median of those that ended in the last 7 days. An opened ticket gives its own. A ticket has none when it was dropped, lacks either exact time (one started before 0.29), or ends before it starts (its issue was created after the work started).
- **Reference** (bottom): the closed decisions and actions.

### Watch

- `tracker watch [name]` prints each change as one line; `!` marks what needs you. One watcher per tracker.
- Only you start it: in a terminal, or with the watch skill ([Usage](#usage) step 5), which makes that session a read-only overseer. The CLI refuses `watch` in an agent session without that grant, and every write in a session with it.

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

- The rules for a change (tests, lint, changelog, release, the format's contract, the agent's texts) are in [CLAUDE.md](CLAUDE.md).
- Run from a checkout in Claude Code: `claude --plugin-dir <path to this folder>`, then `/reload-plugins` after an edit. In Codex, install through the marketplace. Codex caches an installed copy by version: to try a change, raise the manifest version in your checkout, run `codex plugin add work-tracker@<marketplace-name>` again, and put the version back before you commit.
- An open viewer restarts itself when `scripts/tracker/` changes, and an open page reloads when `viewer/` changes.

### Code

`scripts/tracker/`: stdlib only, run by `bin/tracker` and `scripts/hook.sh`. One module per concern; each imports only the modules before it:

1. `markdown`: the file format as text (frontmatter, sections, link lines)
2. `model`: contract constants, records, dependencies, body edits, the lock. A new record kind, value form or scale is one entry in `KINDS`, `FORMS` or `SCALES`: each one's docstring or comment says what else follows from it
3. `git`
4. `session`: the session's tracker, branch matching, commit marks and handoffs, the sessions running now
5. `contract`: `check`, `rules`, `migrate`
6. `views`: text views and the brief
7. `github`: the GitHub adapter. `sync` reads PR state with `gh` and maps GitHub's words onto the tracker's (`PR_FACTS`); `model.apply_prs` writes them, and `whose_move` reads only those. Another forge is another adapter
8. `watcher`: `tracker watch` and the user's grant; archiving and deleting a tracker
9. `viewer`: the page and its server (look: `viewer/page.html`, `style.css`, `app.js`). Markup is `Html`, which escapes the text put into it; the sequence's columns are its `Column` table
10. `hooks`
11. `cli`

### Evals

`evals/` holds [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals) cases. They check that the skill, the brief and the hook texts lead the model to record work through the CLI and to answer from the tracker's views; each case's `prompt.md` says what it checks. Each run is a full Claude session, billed to your plan: run them after a significant change to those texts, not as a routine check. `evals/run.sh` runs them (its header explains the fixed options):

```
evals/run.sh quick <tag>...   # the cases with any of the tags, once each, on Sonnet
evals/run.sh quick all        # every case, once each, on Sonnet
evals/run.sh release          # every case, 3 runs each (1 when its prompt.md sets `runs: 1`), on Opus: before a release
```

Options after the mode's words go to `claude plugin eval` as they are, such as `--case <name>` or `--keep-temp`.


- Tags name the area a case tests. Pick the tags of the text you changed:
  - `write`: the write commands and their output, the skill's Write table
  - `log`: what is logged: the commit hooks, `step` messages
  - `decision`: `decide`, the decision bar, the question hook
  - `issue`: issue fields
  - `read`: `index`, `ready`, `context` and what can start
  - the skill's other parts and the brief's protocol reach every case: `quick all`
- A case sets `runs: 1` only when it is one plain command that passed in every full run.
- Each case's `scaffold.sh` sources `evals/lib/demo-tracker.sh`, the workspace every case starts from (its header says what it holds).
- On macOS with only the Xcode `git` (`/usr/bin/git`), git cannot run in the sandbox, so the cases leave git to the scaffold and the hooks.
