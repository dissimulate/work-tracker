# Changelog

The changes a user of the plugin sees, per release. A commit with such a change adds its line under Unreleased;
`scripts/release.py` moves those lines to the release's section (README "Develop").

## Unreleased

### Added

- Viewer: a tracker menu (top left) lists every tracker; pick one to open it. Its ⋯ menu archives it, or deletes it to the system's trash after a confirm. A tracker with an agent session or a watch on it cannot be deleted.
- `tracker delete <slug>` moves a tracker to the system's trash from a terminal. It refuses in an agent session.
- `tracker archive <slug>` puts a tracker away: no list, lookup, hook or sync reads it, and the viewer still opens it. `tracker unarchive <slug>` brings it back, and `tracker list --archived` lists them. The viewer's tracker menu lists the archived trackers in a closed section at its bottom.
- Viewer: a page whose tracker was deleted says so instead of reloading.
- Actions: tasks for you that the agent cannot or should not do, such as a follow-up with a coworker. The agent asks before it adds one (`tracker act`), and closes it when you say it is done. `tracker actions` lists the open ones, and `tracker show A-<n>` prints one with its notes, one line per fact. An action's id works wherever a record's id does: `tracker context A-<n>`, `tracker history --ref A-<n>`, `tracker open A-<n>`.
- An action can have a due day (`tracker act --due`); the open actions list the soonest due first.
- Viewer: a Your actions section above Now lists the open actions, one line each with its due day and Done and Drop buttons, opening to its notes. Reference lists the closed ones beside the closed decisions.
- `tracker watch` marks a new action as one that needs you.
- `tracker act A-<n> --title` gives an action a new text.
- A ticket can have a size from 1 (XS) to 5 (XL): `tracker new --size` and `tracker set <id> size=<n>`. The agent takes it from the issue's estimate when it has one (`tracker issue --size`), or else gives its best estimate, and leaves it empty when it cannot estimate it reliably. The viewer shows it in a Size column.
- A tracker's README can hold `## Instructions`: standing rules for the agent on this work, such as an action to add when a kind of ticket's PR opens. Every session's brief prints it.
- Any ticket or decision can have a due day, like an action: `tracker new --due`, `tracker set <id> due=<YYYY-MM-DD>`. The agent sets one only when you or the issue tracker give it; `tracker issue --due` records the issue's.
- Tickets, decisions and actions keep when they closed (`closed_at`), and a ticket when it was added (`created_at`).
- An action can block tickets, as a decision can: `tracker act "<text>" --blocks <ids>`, `tracker act A-<n> --blocks <ids>` or `tracker wait <id> on A-<n>`. A ticket under way that waits on one shows it as your move, and closing the action names the tickets it no longer blocks. The brief and the viewer show what each open action blocks. `tracker act A-<n> --unref <ids>` drops ids from what it concerns.

### Changed

- Viewer: the Step column draws the dependency graph instead of a number: a dot per ticket in the column of its step, and a line to each ticket that waits on it, routed beside the other dots. A line that a longer chain already implies is left out. It is drawn again for any sort or filter.
- Viewer: the graph shows the status that row colours showed before: a dot is blue when ready, amber when under way or stackable, red when blocked, and a ring when closed; a line is red while it blocks, amber while the ticket waited on is under way and green once it is closed. Ticket titles are plain text, and a ticket's id takes its dot's colour. Hover a row to see only the chains it waits on and unblocks.
- Viewer: the sequence starts in a depth-first dependency order, so a chain of tickets stays together; Step puts it back.
- Viewer: the sequence has no Time column. An opened ticket gives its wait and cycle time on its first line.
- Viewer: a hovered sequence row takes a fainter fill.
- Viewer: the toolbar (tracker menu, Refresh, theme) stays at the top as the page scrolls.
- Viewer: the Status column comes before Group.
- Viewer: a closed ticket's row is dimmed in every column but Step, not only its title.
- Viewer: a status has one colour everywhere, its tag's: in its tag, as text (a dependency, a decision), as a ticket's id and in the graph.
- A new session on a feature branch that a ticket of one tracker names links to that tracker by itself, without the question. On a default branch such as `main`, or when the match is less sure, the session still asks. To undo, tell the agent the session is not that work.
- After `/clear`, the new session stays on the tracker of the session it replaces, with its `--on` ticket. No need to link again.
- `tracker start --decline` also unlinks a session that linked by itself.
- The brief is shorter. It lists more than 3 closed tickets of a branch as a count (`also 9 done`). It gives the README's Context documents without their URLs (`tracker show tracker --section context` has them). It shows `check` warnings only for this session's tickets and the README, and errors as a count; `tracker check` has the rest.
- Issue fields: an open ticket's fields are read once, not again each day. The viewer's Refresh still has them read again. The request in the brief is one line, with this session's tickets first; `tracker issue --due` says how to record them.
- Viewer: the server keeps one port (7316, `TRACKER_VIEWER_PORT`), so bookmarks and open pages keep working: `http://127.0.0.1:7316/` lists the trackers.
- Viewer: a tracked agent session starts the server, and the server runs while an agent session is on a tracker; no need to ask the agent to open a page first. A page reloads itself when the server is back.
- Every time the tracker writes is UTC to the second, in a key that ends `_at`: `updated` is now `updated_at`, `opened` and the README's `created` are now `created_at`, and `issue_created` is now `issue_created_at`. Run `tracker migrate` to rename them (format 3). The views show each time as its day.
- `tracker issue` with no id lists the tickets whose issue fields are to read (before: `tracker issue --due`). `--due` now records the issue's due day.
- The agent maps an issue's priority and estimate by what they mean on that issue tracker's scale, not by a raw number from its API. A value that means none ("No priority") leaves the ticket's value empty. A size is an amount of work (XS an hour or two to XL more than a week), so a time estimate maps too.
- Viewer: the dependency graph's lines are always as faded as a closed ticket's row.
- Each write logs what the history needs, so the agent runs no `tracker log` after one. A status change through `tracker set` or `tracker step --done` writes its own log line. `tracker add`, `put` and `drop` take `--why`, which logs why a section changes; a started ticket's Plan changes only with it.
- Any write that leaves a ticket with nothing to wait on says so, `tracker wait <id> off` too. A write no longer ends with "log line written".

### Fixed

- Viewer: a tracker that spans repos lists them under its title as `a/x, b/y`, not as a Python list.

## 0.30.0 - 2026-10-08

### Added

- Viewer: a sun/moon button switches themes and saves the choice locally; the system theme is the default.
- `tracker history` prints the log's last lines, filtered by `--ref` and `--since`; `tracker show log` prints the same.
- `tracker attach <name> --append -` adds text from stdin to an evidence file and logs it.
- `put` and `add` make a README section of the work's own when no section has the name.

### Changed

- A ticket's priority is a number from 0 (most urgent) to 4 (least), the same for every issue tracker; the agent maps an issue's level onto it. Any ticket can have one: `tracker new --priority` and `tracker set <id> priority=<n>`. Run `tracker migrate`: it turns the words recorded before into numbers, and an unknown word is read again.
- Viewer: sequence columns fit their visible contents, leaving the remaining width for ticket titles.
- Viewer: clearer spacing and headings, charcoal dark surfaces, and consistent solid pastel status tags without dots.
- Viewer: a red move tag marks your turn instead of a blue left border; narrow layouts and reduced motion are improved.
- Each write ends with one line that says what it did: the log line it wrote and the `check` result.
- `step`, `log` and `add carry` take longer text: 400 characters for `next`, a summary and a log line, 600 for a Carry forward bullet.
- `tracker context <tracker name>` prints the tracker's open work.
- The slash command takes a tracker name as `start`, and `view` as `open`.
- A closed ticket asks for its issue fields only once, and only when it has a recorded start.
- A session hears of a change to its own tickets only when the ticket becomes its own or ends.
- The brief, the skill and the hook lines say that a `step` message leaves out pushes, merges, review rounds and test runs.
- Viewer: wait and cycle time share a Time column, and waits on and unblocks share a Deps column. The columns hide by the table's width.
- Viewer: every field is escaped by default.

### Fixed

- Viewer: long live-status text no longer crowds the toolbar controls; hover over it to read the full status.
- Tracker updates and attachments no longer write through destination or temporary-file symlinks.
- File replacements preserve existing permissions; new replacement files and copied attachments are owner-only.
- In Codex, a hand edit of a tracker file with `apply_patch` counts as the session's own: the edit hook now runs for it.
- The hooks log only a branch's own commits, not those that a merge from the default branch brings in.
- `put` runs as a write: under the lock, with its `check` line, and not in a watching session.

## 0.29.0 - 2026-10-08

### Added

- Wait time (issue created to started) and cycle time (started to PR merged), in `tracker index` and in the viewer's
  sortable columns. The first `set status=in-progress` from todo records `started_at`.

## 0.28.0 - 2026-10-08

### Added

- `tracker issue <id> --priority ... --created ...` records a ticket's issue fields; `tracker issue --due` lists the
  tickets whose fields are due, and the brief asks for them.
- Viewer: a Priority column, and a Refresh button that pulls PR state and asks the next prompt for issue fields.

## 0.27.0 - 2026-10-08

### Added

- Viewer: sort the sequence by any column. The sort is in the address, so a reload or a shared link keeps it.

## 0.26.0 - 2026-10-05

### Added

- Codex support: host-aware session ids, command prefixes and session-end handling.
- The CLI and the viewer find the sessions running now from recent hook activity.

## 0.25.2 - 2026-09-29

### Fixed

- `sync`: a fork's PR from a branch with a ticket's branch name no longer becomes the ticket's PR.
- A tracker slug is one folder name in the tracker home, never a path.
- An edit after a section heading in other case no longer adds a second section.

## 0.25.1 - 2026-09-29

### Fixed

- Viewer: a URL inside a link can no longer add attributes to the page (stored XSS). Pages send a CSP, and
  cross-site requests get 403.
- Python starts with `-I`, so a module in the user's project cannot replace a stdlib module.

## 0.25.0 - 2026-09-29

### Added

- First release: Markdown tickets, dependencies and decisions, a live browser viewer, and hooks that keep the model
  reading and updating the tracker.
