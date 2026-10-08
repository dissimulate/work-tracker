# Changelog

The changes a user of the plugin sees, per release. A commit with such a change adds its line under Unreleased;
`scripts/release.py` moves those lines to the release's section (README "Develop").

## Unreleased

### Added

- Viewer: a sun/moon button switches themes and saves the choice locally; the system theme is the default.
- `tracker history` prints the log's last lines, filtered by `--ref` and `--since`; `tracker show log` prints the same.
- `tracker attach <name> --append -` adds text from stdin to an evidence file and logs it.
- `put` and `add` make a README section of the work's own when no section has the name.

### Changed

- Viewer: sequence columns fit their visible contents, leaving the remaining width for ticket titles.
- Viewer: clearer spacing and headings, charcoal dark surfaces, and consistent solid pastel status tags without dots.
- Viewer: a red move tag marks your turn instead of a blue left border; narrow layouts and reduced motion are improved.
- Each write ends with one line that says what it did: the log line it wrote and the `check` result.
- `step`, `log` and `add carry` take longer text: 400 characters for `next`, a summary and a log line, 600 for a
  Carry forward bullet.
- `tracker context <tracker name>` prints the tracker's open work.
- The slash command takes a tracker name as `start`, and `view` as `open`.
- A closed ticket asks for its issue fields only once, and only when it has a recorded start.
- A session hears of a change to its own tickets only when the ticket becomes its own or ends.
- The brief, the skill and the hook lines say that a `step` message leaves out pushes, merges, review rounds and test
  runs.
- Viewer: wait and cycle time share a Time column, and waits on and unblocks share a Deps column. The columns hide by
  the table's width.
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
