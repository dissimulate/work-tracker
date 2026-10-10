# Work tracker reference

The `tracker` skill points here. `tracker rules` is the full contract: every key and what writes it, the statuses, labels, sections and the decision bar; `tracker check` enforces it.

## Files and the home of each fact

| File | Holds |
|---|---|
| `README.md` | the work: Context (the documents it answers to), Goal, Scope, and optionally Instructions (this work's standing rules for you, printed in every brief); ≤ ~2K tokens, rewritten, never appended to |
| `tickets/<ID>.md` | frontmatter = state (`status`, `branch`, `next`, `summary`, `depends_on`, PR keys, issue keys); body = Plan, Carry forward, Links |
| `decisions/D-<n>.md` | a direction decision: Question, Options, and once closed, Resolution |
| `actions/A-<n>.md` | a task for the user that you cannot or should not do: its text, the ids it concerns, notes |
| `log.md` | dated one-line history, append-only |
| `evidence/` | files the records cite: runs, measurements, scripts |
| `.state.json` | machine state, never edited: per repo and branch the mark up to which the hooks logged its commits, the handoff and PR lookups; per worktree the `use` choice. Stale entries go by themselves |

**One source per fact.** Each fact has one home; every view is computed from it, so write it there once:

- **Order and blockers**: the waiting ticket's `depends_on` (the README may keep *why* an order was chosen); an open PR based on another ticket's branch also waits on it (from the PR, not written). Ready, blocked, unblocks and the critical path are computed.
- **Where a ticket starts**: computed from the same. A todo ticket that waits only on tickets under way can start stacked on their branch; `tracker ready` and `context` name it. Do not write the base into the Plan, `next` or the log.
- **A branch's tickets**: each ticket's `branch`. A branch holds any number; the session works on the ones under way (in progress or in review).
- **PR state**: written by `sync`. Once a ticket is in progress, its PR shows it in review or merged. You set only the work status.
- **Whose move**: computed for each ticket under way from its PR's reviews, checks and merge state (`sync`) and its open decisions and external blockers; the brief's `move:` line, `tracker index` and the viewer show it. `next` holds your own next action: do not write a wait on a reviewer into it.
- **A decision's answer**: its Resolution. Tickets show it through `context`.
- **Issue-tracker id**: the ticket's `- Issue: [PROJ-12 Title](url)` line; the id also finds the ticket.
- **Priority**: 0 (most urgent) to 4 (least), the same for every issue tracker. With an Issue link it is the issue's: put its tracker's levels in order onto 0-4 (Highest or Urgent 0, High 1, Medium 2, Low 3, Lowest 4). Without one, set your own when you add the ticket. `tracker rules` gives the rule.
- **Issue fields** (`priority`, `issue_created`): the issue tracker holds them; `tracker issue` records what you read there. The tracker cannot read an issue tracker, so the brief, or a prompt after the viewer's Refresh, names the tickets whose fields are due.
- **Wait and cycle time**: computed from each ticket's times. Wait: issue created → started (`started_at`, which `set status=in-progress` records). Cycle: started → PR merged. `tracker index` (the lines under its headline) and the viewer show them: quote them, do not work them out from the files.
- **Unfinished work between sessions**: the branch's handoff, until the next `step`.
- **Build detail**: the PR and commits. The hooks log each commit on the branch of a ticket under way: do not log a commit again.

Link lines (README Context, ticket Links) read `- Label: [title](url) — why it matters`. When a Context document governs a choice you are making, open it and work from it.

## View

`tracker open [id]` opens the live viewer for the user. Its Your actions section, above Now, lists the user's open actions, each with Done and Drop. Its Now section shows the tickets under way, each branch's handoff and how long ago its commits were last logged. Its Refresh (top right) pulls PR state at once and asks the next prompt for the due issue fields.

## New tracker or migration

- **New**: `tracker init <slug> --title "..." --owner <name> [--repo owner/name[,owner/other]]` (`--repo` enables GitHub sync). Fill `README.md`: Context first (every document linked from the parent issue or brief), then Goal and Scope. Add tickets with `tracker new <ID> --title "..." [--group <label>] [--depends <ids>] [--priority <0-4>]`; a group is only a label.
- **Older tracker**: when `tracker check` says so, run `tracker migrate --dry-run`, then `tracker migrate` (it backs up first).
- **A plan from elsewhere**: plan text → Plan; facts later tickets rely on → Carry forward; open questions and blockers → decisions; dated notes → one log line each, with a link to the detail; measurement files → `attach`.
