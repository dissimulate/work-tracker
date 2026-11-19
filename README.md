# work-tracker

This plugin is a work in progress.

A Claude Code and Codex plugin that keeps the plan and progress of a piece of work (a feature, a project, a migration) in plain Markdown on your machine. The agent reads it at the start of each session and updates it as it works, so the next session continues where the last one stopped.

- Tickets with dependencies, direction decisions and a dated log.
- Actions: tasks for you that the agent cannot or should not do, such as a follow-up with a coworker.
- Hooks log your commits and pull PR state from GitHub.
- A live native view shows the work under way and whose move each ticket waits on: a tab in [Herdr](https://herdr.dev), or its own window.

<img width="2424" height="3246" alt="demo-notification-prefs" src="https://github.com/user-attachments/assets/14519bf1-8e15-4c64-b19e-d05916b81ebe" />


## Usage

Talk to the agent. It runs the `tracker` CLI for you.

1. **Create a tracker.** Ask the agent, for example: "Make a tracker for the payments rework in acme/api, with tickets for the schema, the API and the admin page." Give it the documents the work answers to (spec, issue, design): they go in the tracker's Context. Name the GitHub repo, so PR state syncs.
2. **Start a session on it.** Say "work on the payments tracker", or type `/work-tracker:tracker start payments` in Claude Code or `$work-tracker:tracker start payments` in Codex. The agent gets a brief: where the work is, what is under way, what it waits on.
   - A new session on a feature branch that a ticket of one tracker names, in a repo that tracker names, links to that tracker by itself, and says so. To undo, tell the agent it is not that work: it unlinks, and the branch gets no link or offer for a day.
   - Anywhere else on a branch with an open ticket (a default branch such as `main`, a branch whose name only holds a ticket id, a branch of tickets in more than one tracker), the session asks at your first message whether to link it. Answer yes, or "Not now" (no offer on that branch for a day).
   - After `/clear`, the new session stays on the tracker (and the `--on` ticket) of the session it replaces.
3. **Work as usual.** The agent records steps, decisions and pauses as they happen. The hooks log each commit and keep PR state current. You write nothing yourself.
4. **See it.** Ask the agent to open the tracker, or run `tracker open`. The view updates live.
5. **Watch the work (optional).** In a session, type `/work-tracker:watch payments <what to do>` in Claude Code or `$work-tracker:watch payments <what to do>` in Codex, such as "tell me when a review comes back". The session gets each change to the tracker as it happens, and does that task. A skill or instruction of yours can start it too. Invoke the same skill with `stop` to end it. In a terminal: `tracker watch payments`.

`/work-tracker:tracker <command>` runs any `tracker` command, for example `/work-tracker:tracker index --active`.

### Useful commands

| Command | Does |
|---|---|
| `tracker list` | all trackers (`--archived`: the archived ones too) |
| `tracker index --active` | the tickets under way, whose move each waits on, the open decisions, the wait and cycle times |
| `tracker ready` | what can start now, the most urgent first, and from which branch |
| `tracker context <id>` | one ticket or decision in full, with what it builds on |
| `tracker decisions --all` | the decisions, open and settled |
| `tracker actions` | your open actions (`--all`: the closed ones too) |
| `tracker find <text>` | search a tracker (`--all`: every tracker) |
| `tracker history` | the log's last lines (`--ref <ids>`, `--since <date>`, `--kind <kinds>`) |
| `tracker submit <id> --what plan` | hand a ticket's plan, or with `--what code` its built code, to its reviewer: the coordinator, else you |
| `tracker review <id> --what plan --verdict pass` | record a review of it; `--verdict changes` takes a `--finding` per change asked for |
| `tracker open [id]` | the live view: a tab in Herdr when Herdr is open, else its own window; `--window` for the window also when Herdr is open |
| `tracker install-window` | download what the view's own window needs, once |
| `tracker install-mod` | install the view as a Herdr mod, once: each later plugin version updates it |
| `tracker archive <slug>` | put a tracker away: out of every list, lookup and hook, still in the view; `tracker unarchive <slug>` brings it back |
| `tracker delete <slug>` | move a tracker's folder, archived or not, to the system's trash (only in a terminal; refused while a session or a watch is on it) |
| `tracker issue` | the issue fields to read and, with issue updates on, the updates to write, with how to record them |
| `tracker check` | problems in the tracker's files, then what is stuck |
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
- Optional, for the live view: [Herdr](https://herdr.dev) with the view's mod (`tracker install-mod`), or the view's own window (`tracker install-window`: macOS, x86-64 Linux or x86-64 Windows). Without either, `tracker index` and `tracker context <id>` show the same facts.

## How it works

### Data

Each tracker is a folder in `~/.claude/trackers/<slug>/` (`TRACKER_HOME` changes the root). The folder is outside every repo, so all worktrees and sessions share one copy. Writes take a lock, so sessions that run at the same time do not overwrite each other. For history, run `git init` in the folder. On macOS and Linux, Python keeps its compiled code in `$TRACKER_HOME/.pycache/`; you can delete that folder at any time.

| File | Holds |
|---|---|
| `README.md` | the work: Context, Goal, Scope; optionally Instructions, this work's standing rules for the agent, which every brief prints |
| `tickets/<ID>.md` | one ticket: status, branch, next action, dependencies, priority, size, due day, its times; Plan, Carry forward (facts later tickets need), Links |
| `decisions/D-<n>.md` | one direction decision: Question, Options (one line per choice, one may be marked recommended), Notes, Resolution (the day, who decided and how it was entered, and the answer) |
| `actions/A-<n>.md` | one action for you: what to do, the tickets or decisions it concerns, notes |
| `events.jsonl` | the log: one event per change, each with its time, the session and the plugin version that wrote it, and how it was entered: in the view, by an agent, or at a terminal. Only the tool writes it |
| `log.md` | the log as dated lines, for reading. Editing it changes nothing the views show, except what `tracker find` finds |
| `evidence/` | files the records cite: runs, measurements |
| `.state.json` | machine state. Do not edit |

`tracker rules` prints the full format: every key and what writes it, and each rule below in full.

- **Who decided.** A decision's resolution says who settled it and how it was entered: you in the view; you through an agent, which names you (`--by`); an agent that names the source that already answered it (`--source`: the spec, a settled decision, the code); or, from a terminal with no session, unattributed, never taken for a person. An agent that resolves a decision must name one of the two. Who entered it comes from the session's environment, so it guards against mistakes, not against an agent that sets out to deceive.
- **Actions** are tasks for you that the agent cannot or should not do: talk to a person, get an access or a sign-off. The agent asks before it adds one, and closes it when you say it is done or no longer needed. An action can have a due day, and can block tickets as an open decision does. The agent asks you about one past its due day, or with none, open longer than a week.
- **Priority** is a number from 0 (most urgent) to 4 (least), shown as P0 to P4. **Size** is a number from 1 (XS) to 5 (XL), as an amount of work: XS an hour or two, S about a day, M a few days, L about a week, XL more. The scales are the same for every issue tracker, so values compare, sort and add up across them.
- **Issue fields.** A ticket with an `Issue:` link takes its issue's priority, estimate (as its size), due day and creation time. The tracker never calls an issue tracker: the agent reads the issue with its own tool for it (such as an MCP server you have signed in to), maps each value by what it means on that issue tracker's scale (High is 1; 5 Fibonacci points are L), and records it with `tracker issue`. With no such tool, the fields stay empty.
- **Issue updates.** Off until you run `tracker set tracker issue_updates=on`. Then the agent writes each linked ticket's changes to its issue with the same tool: a new stage (started, in review, merged, closed, dropped, reopened) moves the issue to the matching state of its workflow, and a closed ticket's summary and each decision settled on it become comments. It writes in its own words and never names the tracker. It is asked right after the change, in each brief, and at your next message. When you turn them on, each ticket already started or closed is due once for its status; decisions settled before then are not written. `issue_updates=off` stops them; on again, what changed while off is due. The record of what was written is in `.state.json`, on this machine only: a second machine on the same tracker writes the same updates again.
- **The agent's own values.** On a ticket with no issue value, the agent sets priority and size as its best estimate, or leaves them empty when it cannot estimate them reliably. A due day (`YYYY-MM-DD`, on any ticket, decision or action) is only a day that you or the issue tracker gave.
- **Times**: a key that ends `_at` holds a time in UTC to the second, such as `started_at` or `merged_at`. The views show each as its day.
- Nothing leaves your machine except the `gh` calls to GitHub and, with issue updates on, what the agent writes to issues with its own tool. Nothing listens on a port.

### Tickets, branches and sessions

- A ticket's status is `todo`, `in-progress`, `done` or `dropped`. Its PR adds `in-review`, and once merged makes it `done`.
- `depends_on` sets the order and the blockers. What is ready or blocked, and the branch a ticket can start from (stacked on work under way), are computed.
- The branch names the session's tickets: each ticket whose `branch` it is, a `tracker use <id>` choice, a ticket id or Issue id in the branch name, or the branch's PR.
- Many sessions can share one tracker. `tracker start <slug> --on <id>` puts a session on one ticket of a shared branch.
- The tracker never changes git.
- **Isolation**: the tracker is private to the work. Outside it (code, commits, branch names, PRs, issues) the agent writes each fact in its own words and cites only real ids (an Issue id, a PR number, a URL), never the tracker's ids or name.

### Hooks

Each hook runs `scripts/hook.sh`, which filters the event in shell first: Python starts only when the hook can add something. A hook never blocks the session.

| Event | Does |
|---|---|
| SessionStart | Gives the session its command prefix or shell environment. With a tracker: logs new commits and injects the brief, with the issue fields to read and the issue updates to write, then syncs PR state in the background (at most every 10 min): the next message reports what changed. With none: links the session, or has the agent offer the link ([Usage](#usage) step 2). |
| UserPromptSubmit | Reports what other sessions or GitHub changed on the session's tickets since the brief: a move that became yours, a ticket that ended, a dependency's state or Carry forward, a decision; a watching session has these from its watch. Once each: an issue update newly due, the issue fields the view's Refresh asked for. A state line with the work the tracker may not show (unlogged commits, uncommitted files): with a ticket under way, on the first message and every 5th; with none, only when there is such work. With a ticket under way, starts a stale GitHub sync in the background. Handles `/work-tracker:watch`. |
| PostToolUse | One hook for every tool, which starts Python only when one of these applies. Bash: after a commit, logs it, and once per next action asks whether a step ended; after `git push` or `gh pr …`, records the branch on its ticket and syncs PR state. Edit, Write, MultiEdit, apply_patch: counts a hand edit of a tracker file as this session's own. AskUserQuestion, request_user_input: asks the agent to record the answer when it settles a direction decision. Any tool: ends that agent's "needs you" mark, and logs that its wait ended. |
| SubagentStart | Tells a subagent the tracker and ticket, and that it writes nothing to them. |
| PermissionRequest, PreToolUse (AskUserQuestion, request_user_input) | Marks the session as needing you, and logs that the agent began to wait on you. The wait ends when that agent goes on, or at the session's next message, turn end, start (a resume, a compaction) or end, which ends every wait. Neither is in `tracker history` or `log.md` unless `--kind` names it. |
| PostToolUseFailure, SubagentStop | Ends that agent's mark and logs that its wait ended (with the tool's error after a failed tool: a refusal or a failure, which the hook cannot tell apart), in a shell check that starts no Python otherwise. The session needs you until each agent that asked goes on. |
| Stop | Logs the commits no other hook saw. |
| StopFailure | Marks the session idle after a failed turn. |
| SessionEnd | Logs remaining commits and ends the session's activity in the view. On `/clear`, leaves the session's tracker for the next session in the same directory. |

### View

`tracker open [id]` shows the tracker, and opens the record `id` names. The view has no server: it runs the `tracker` CLI for all it reads and changes (`tracker data`, `tracker list --json`), when the tracker or its sessions change and at least every 15 s. Herdr watches the folder; in its own window, `tracker await-change` waits for that. While it shows, the view syncs PR state when its data is older than `TRACKER_SYNC`, also when it opens.

- **Hosts**:
  - **Herdr**: a tab that the `work-tracker` mod draws; `tracker open` shows the tab if it is there. `tracker install-mod` installs the mod; only you run it, as the mod runs programs. Each new plugin version updates it at its first session on a tracker or `tracker open`. To put the view in a layout, use a block with `{ "mod": "work-tracker" }`; a layout that reads a mod's `panel` field (`ui/mod.json`), such as Bettr, shows it as a panel with its own button.
  - **Follow**: a view with no tracker asked for follows the focused agent: it shows the tracker that agent's session is linked to, or says that there is none, with the switcher still there. A tracker you pick stays until the agent or its tracker changes; the tab of `tracker open` stays on its tracker.
  - **Another Herdr machine**: an agent there has its tracker there. The view runs that machine's CLI through Herdr, its switcher lists that machine's trackers, and its bar names the machine. That machine needs the same plugin version and a session that has used a tracker there, which records the CLI. Its tracker updates every 15 s, not at once, and an evidence link opens on that machine's screen.
  - **Own window**: with no Herdr window open, no mod installed, or `--window`, `tracker open` runs `gpui-shell run --dir <plugin>/ui`, a new window each time, and says why if `gpui-shell` refuses at once. It uses the [`gpui-shell`](https://github.com/longbridge/gpui-kit/releases) that `tracker install-window` downloads to `$TRACKER_HOME/.gpui-shell/` and checks by hash, else the one on your PATH, which must be the version `ui/gpui-shell.json` names: `gpui-shell` is third-party and not yet stable.
- **Tracker menu** (top left) lists every tracker, the archived ones in a closed section at the bottom. A tracker's ⋯ has Archive (Unarchive) and Delete, both off while an agent session or a watch is on the tracker.
  - Archive moves the tracker to `$TRACKER_HOME/.archive/`: no list, lookup, hook or GitHub sync reads it. It still opens, read-only. `tracker init` refuses its slug.
  - Delete moves the tracker's folder to the system's trash after you confirm; restore it from there. If the trash refuses the folder, the folder stays and the view shows why, until you press that line or make another change.
- **Refresh** (top right) pulls PR state from GitHub at once, and has the agent read the issue fields again at your next message in a session on the tracker. The live line beside it says when they were last read.
- **Your actions**: your open actions, soonest due first, with Done and Drop. A due day is red once past.
- **Current**: the tickets under way (your move first), each branch's handoff, and the agent sessions on the tracker. Each row's dot says what its sessions do: red when one needs you, green while one works, yellow while they are idle, a grey ring with none. A session is busy from your message to its turn's end. An interrupted turn can show busy until the next event, and a prompt you end with Esc can show "needs you" until then.
- **Sequence**: the tickets in dependency order, depth first so a chain stays together, dropped tickets last. The Step column draws the dependency graph:
  - a dot per ticket: blue when ready, green in progress, yellow in review or when it can stack on work under way, red when blocked, a grey ring when closed or dropped. The ticket's id takes the same colour.
  - a line from each ticket to each ticket that waits on it: red while it blocks, green while in progress, yellow while in review, grey once closed.
  - Hover a row to see only what its ticket waits on and unblocks.
  - A column heading sorts by that column, and a second press reverses it. Step puts the dependency order back. The columns are Status, Priority, Size, Group, Waits on and Unblocks; a narrow view leaves them out from the right as they stop fitting, and keeps Status.
- **A record** (a ticket, decision or action) opens in a modal over the page when you click it, and a press outside it or Escape closes it: what it waits on and unblocks, its facts and links, its body. An open decision lists its options: click one to answer with it, or write your own answer in the box below them and press Answer. The answer closes the decision, as `tracker decide D-<n> --resolve` does, and the agents see it with the next change. A link to `evidence/` opens the file in the system's app when that kind of file runs no code, and otherwise shows it in its folder. Only `http` and `https` links leave the view.
- **Wait time** runs from when a ticket's issue was created (`issue_created_at`) to when the ticket started (`started_at`, which its first `tracker set <id> status=in-progress` records). **Cycle time** runs from that start to when its PR merged (`merged_at`). Above the filters, a line per time gives the median, the fastest ticket, and the median of those that ended in the last 7 days. An opened ticket gives its own. A ticket has none when it was dropped, lacks either exact time, or ends before it starts (its issue was created after the work started).
- **Reference** (bottom): the closed decisions and actions, the rest of the README, the log.

### Watch

- `tracker watch [name]` prints each change as one line; `!` marks what needs a human, such as a question with no one else to answer it, also an agent that has asked for a permission or an answer for 30 s. It also says when an agent ends its turn.
- A `stuck <id>` line, once per item: a submission not reviewed or a question not answered past its deadline (`TRACKER_DEADLINE_SUBMISSION`, `TRACKER_DEADLINE_QUESTION`), or at once when its ticket is under way and no session is on it. Only one that is the coordinator's or a session's to answer turns stuck, and is then yours: one that is yours already is simply yours. A wait on you past 30 s is stuck too, and the watch says it as that agent's line. Stuck is worked out from the log and the clock each time it is read, so nothing has to be running: the view's data and `tracker check` show it too. Several sessions can watch one tracker: one watch per session.
- `--only <kinds>` prints only those kinds of change (`tracker watch --help` lists them). `--hold <seconds>` prints the changes that do not need you as one batch, at most that often; a `!` line comes at once.
- `tracker agents [name]` lists the agent sessions on a tracker: status, tickets, branch, folder, its Claude Code session name and Herdr terminal when it has them, and its transcript file.
- `--as coordinator`, in an agent session: the session takes the tracker's coordinator role while its watch runs. A decision with no owner (or the owner `coordinator`) is then the coordinator's move, to answer from the spec, a settled decision or the code, or to pass to a person with `tracker decide D-<n> --owner <name>`; when no coordinator is watching, it is yours. On the coordinator's own watch, `!` also marks a question another session opened or changed that is the coordinator's to triage. A decision's owner can also be a ticket, meaning the session on it while it is under way.
- In an agent session, the watch skill starts it ([Usage](#usage) step 5): when you invoke it, or when a skill or instruction of yours tells the agent to watch. The agent never starts it on its own. The watching session writes to the tracker, and can be on one, as any session. `tracker watch --stop`, or the skill with `stop`, ends it.

### Upgrading a tracker

The `schema` key in a tracker's README names its format. A tracker in an older format, archived or not, migrates itself the first time a command or hook of a newer version runs. It backs up first to `$TRACKER_HOME/.backups/<slug>-schema<n>/`, all but its `evidence/` folder (no migration changes it), and adds a log line. If it fails, the tracker stays as it was, the next command tries again, and `tracker check` warns. Until it migrates, nothing else writes to it: a command refuses before it changes anything and says what to fix. That is a README error that `check` reports, or the error of the migration step that failed, which each command prints on stderr. A README that cannot be read (a key twice, say) is yours to fix by hand: an agent never edits frontmatter. A command or hook of an older version writes nothing to a tracker in a newer format: a command refuses before it changes anything and says to use the newer version. The hooks of a session on a tracker that takes no write tell the session once, at the first hook that can reach the agent (its start, or the next message you send or commit it makes when the tracker stops taking writes later), and again after compaction or /clear. They ask the agent to record nothing until you act, and then to run `tracker start` for its brief: for a newer format you reload the plugin or restart the session, and for one that could not migrate you fix what `tracker check` reports. A session on no tracker is told of such a tracker on its branch, not linked or offered a link to it. Commands that only read still run, and `tracker here` and a bare `tracker start` say so after the brief. Versions up to 0.32.0 do not refuse, and write their own format: restart each session that runs one.

### Settings

Environment variables. All are optional.

| Variable | Default | Does |
|---|---|---|
| `TRACKER_HOME` | `~/.claude/trackers` | where the trackers are |
| `TRACKER` | none | a tracker slug: commands use it first, the hooks when `tracker start` chose none |
| `TRACKER_NUDGE_EVERY` | 5 | messages between state lines |
| `TRACKER_SYNC` | 120 | seconds (at least 30) between GitHub syncs while the view or a watch is open (a session's hooks sync at most every 10 min) |
| `TRACKER_WATCH_IDLE` | 600 | seconds before an idle agent that waits on you is reported |
| `TRACKER_WATCH_BUSY` | 2700 | seconds a busy agent can record nothing before it is reported |
| `TRACKER_DEADLINE_SUBMISSION` | 1800 | seconds a submitted plan or built code can wait for the coordinator's review, from the later of its submission and the coordinator's start, before it is stuck |
| `TRACKER_DEADLINE_QUESTION` | 1800 | seconds an open decision for the coordinator or a session can wait for its answer, from when it became theirs (opened, passed on, the coordinator's start, the owner ticket's start), before it is stuck |

## Develop

- The rules for a change (tests, lint, changelog, release, the format's contract, the agent's texts) are in [CLAUDE.md](CLAUDE.md).
- Run from a checkout in Claude Code: `claude --plugin-dir <path to this folder>`, then `/reload-plugins` after an edit. In Codex, install through the marketplace. Codex caches an installed copy by version: to try a change, raise the manifest version in your checkout, run `codex plugin add work-tracker@<marketplace-name>` again, and put the version back before you commit.
- The UI: `herdr-app check ui` needs `ui/gpui-shell.json` out of the way (Herdr refuses it), so run `HERDR_APP_HOME=<an empty folder> bin/tracker install-mod`, then `herdr-app check <that folder>/mods/work-tracker`. `bin/tracker install-mod` with no `HERDR_APP_HOME` puts the checkout's view in your Herdr; after that, a session that runs the checkout brings each edit of the mod's files to it, at the next session start or prompt on a tracker. A session of another plugin copy (an installed version) puts its own back at its first hook. A mod file that is a link stays as it is. For the window: `gpui-shell check --dir ui`. A check draws only the first frame, so look at the view too. `ui/model.js` holds what can be tested without a UI: `node --test ui/`.

### Code

`scripts/tracker/`: stdlib only, run by `bin/tracker` and `scripts/hook.sh`. One module per concern; each imports only the modules before it:

1. `system`: this Python and the CLI as processes, the plugin's folder and version, the session id, whether a process runs and which run under it, the system's trash
2. `markdown`: the file format as text (frontmatter, sections, link lines)
3. `store`: writing the tracker's files: atomic writes, JSON files, the lock, the guard on each write
4. `schema`: contract constants and value forms. A new record kind, status, value form or scale is one entry in `KINDS`, `LIFECYCLES`, `FORMS` or `SCALES`: each one's docstring or comment says what else follows from it
5. `model`: records, the `Tracker`, dependencies, the log (its events, and log.md written from them), body edits
6. `moves`: whose move a ticket under way waits on
7. `git`
8. `session`: the session's tracker, branch matching, commit marks and handoffs, the sessions running now, the watches' state and grants, and whether a coordinator watches (`coordinated`)
9. `contract`: `check`, `rules`, `migrate`, and no write into another format
10. `views`: text views and the brief
11. `github`: the GitHub adapter: `sync` reads PR state with `gh` and maps GitHub's words onto the tracker's (`PR_FACTS`). Another forge is another adapter
12. `watcher`: `tracker watch`, and giving or ending a session's grant; `tracker agents`; `tracker await-change`
13. `lifecycle`: archiving and deleting a tracker
14. `viewmodel`: what a UI shows, as data: `tracker data` and `tracker list --json`
15. `ui`: where the UI in `ui/` runs: `tracker open`, `tracker install-window`, `tracker install-mod`, and an installed mod kept at this version
16. `hooks`
17. `commands`: one function per command
18. `cli`: the parser, and what runs around each write command

`ui/`: the view, one codebase (`view.js`, with `model.js` and `style.js`). `main.js` runs it as a Herdr mod, `app.js` in gpui-shell.

### Evals

`evals/` holds [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals) cases. They check that the skill, the brief and the hook texts lead the model to record work through the CLI and to answer from the tracker's views; each case's `prompt.md` says what it checks. Each run is a full Claude session, billed to your plan: [CLAUDE.md](CLAUDE.md) says when to run them. `evals/run.sh` runs them (its header explains the options):

```
evals/run.sh quick <tag>...   # the cases with any of the tags, once each, on Sonnet
evals/run.sh quick all        # every case, once each, on Sonnet
evals/run.sh release          # every case, 3 runs each (1 when its prompt.md sets `runs: 1`), on Opus: before a release
```

- Tags name the area a case tests. Pick the tags of the text you changed:
  - `write`: the write commands and their output, the skill's Write table
  - `log`: what is logged: the commit hooks, `step` messages
  - `decision`: `decide`, the decision bar, the question hook
  - `issue`: issue fields and issue updates
  - `read`: `index`, `ready`, `context` and what can start
  - the skill's other parts and the brief's protocol reach every case: `quick all`
- A case sets `runs: 1` only when it is one plain command that passed in every full run.
- Each case's `scaffold.sh` sources `evals/lib/demo-tracker.sh`, the workspace every case starts from (its header says what it holds).
- On macOS with only the Xcode `git` (`/usr/bin/git`), git cannot run in the sandbox, so the cases leave git to the scaffold and the hooks.
