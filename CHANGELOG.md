# Changelog

The changes a user of the plugin sees, per release. A commit with such a change adds its line under Unreleased;
`scripts/release.py` moves those lines to the release's section (README "Develop").

## Unreleased

### Added

- Viewer: a tracker menu (top left) lists every tracker; pick one to open it. Its ⋯ menu archives it, or deletes it to the system's trash after a confirm. A tracker with an agent session or a watch on it cannot be deleted.
- `tracker delete <slug>` moves a tracker to the system's trash from a terminal. It refuses in an agent session.
- `tracker archive <slug>` puts a tracker away: no list, lookup, hook or sync reads it, and the viewer still opens it. `tracker unarchive <slug>` brings it back, and `tracker list --archived` lists them. The viewer's tracker menu lists the archived trackers in a closed section at its bottom.
- Viewer: a page whose tracker was deleted says so instead of reloading.
- Actions: tasks for you that the agent cannot or should not do, such as a follow-up with a coworker. The agent asks before it adds one (`tracker act`), and closes it when you say it is done. `tracker actions` lists the open ones, and `tracker show A-<n>` prints one with its notes, one line per fact.
- An action can have a due day (`tracker act --due`); the open actions list the soonest due first.
- Viewer: a Your actions section above Now lists the open actions, one line each with its due day and Done and Drop buttons, opening to its notes. Reference lists the closed ones beside the closed decisions.
- `tracker watch` marks a new action as one that needs you.
- `tracker act A-<n> --title` gives an action a new text.
- A tracker's README can hold `## Instructions`: standing rules for the agent on this work, such as an action to add when a kind of ticket's PR opens. Every session's brief prints it.

### Changed

- Viewer: the server keeps one port (7316, `TRACKER_VIEWER_PORT`), so bookmarks and open pages keep working: `http://127.0.0.1:7316/` lists the trackers.
- Viewer: a tracked agent session starts the server, and the server runs while an agent session is on a tracker; no need to ask the agent to open a page first. A page reloads itself when the server is back.

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
