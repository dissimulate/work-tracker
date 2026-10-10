# work-tracker plugin

- Verify with `python3 -m unittest discover tests` (about 10 s, no network, temporary git repos) and `uvx ruff check`.
- Model evals: `evals/run.sh` only. Each run is a full Claude session on the user's plan: run it only when the user asks, after a significant change to the skill, the brief or the hook texts. While iterating, `evals/run.sh quick <tag>...` with the tags of the changed text (README "Evals" maps each tag to its texts); before a release, `evals/run.sh release`.
- A commit with a change that a user of the plugin sees adds a line per change, written for a user, under `## Unreleased` in `CHANGELOG.md`, below its `### Added|Changed|Fixed|Removed` heading. Internal work (evals, tests, refactors, developer docs) adds none.
- Do not bump `version` in `.claude-plugin/plugin.json` for a change. Release only when the user asks, with `scripts/release.py <patch|minor|major>`: `--dry-run` first and show the user the notes, then `--yes`. It writes the changelog section, bumps the version, commits, tags, pushes and makes the GitHub release. A marketplace that pins this plugin to a commit (`sha`) gets the release only through a PR there that moves the pin to the release commit: after the release, ask the user whether to open one.
- Code layout and module order: README "Code". A module imports only the modules before it in that order.
- Each fact of a tracker has one home (a key, a section, the log, the PR) and every view computes it from there. Contract constants in `scripts/tracker/model.py` are the single source; `check`, `rules`, `set`, the templates and the hook texts read them. A format change bumps `SCHEMA` and teaches `migrate` the step.
- Where each text lives:
  - user: README, from "Usage" to "Settings"; developer: README "Develop" and this file
  - agent, in every tracked session: the brief (`views.py`) and the hook texts (`hooks.py`); on demand: `skills/tracker/SKILL.md`, `tracker rules` (`model.py` constants), `--help` (`cli.py`)
  - The brief's protocol is a short copy of the skill's core rules: change both together.
- The tests fail when a backticked `tracker <command> --flag` in the docs, the skill, the templates or the code names a command or flag the CLI lacks: change the parser and the text together.
- Hook output and the brief enter every tracked session's context. Add a line only when it changes what the model does next, keep the brief within the `BRIEF_*` limits in `views.py`, and phrase requests as the action to take.
