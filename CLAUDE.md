# work-tracker plugin

- Verify with `python3 -m unittest discover tests` (about 10 s, no network, temporary git repos) and `uvx ruff check`.
- Model evals: `evals/run.sh` only. Each run is a full Claude session on the user's plan: run it only when the user asks, after a significant change to the skill, the brief or the hook texts. While iterating, `evals/run.sh quick <tag>...` with the tags of the changed text (README "Evals" maps each tag to its texts); before a release, `evals/run.sh release`.
- Ship a change by bumping `version` in `.claude-plugin/plugin.json` (installed copies are cached by version), then push. A marketplace that pins this plugin to a commit (`sha`) gets the release only through a PR there that moves the pin to the pushed commit: after the push, ask the user whether to open one.
- Code layout and module order: README "Code". A module imports only the modules before it in that order.
- Contract constants in `scripts/tracker/model.py` are the single source; `check`, `rules`, `set`, the templates and the hook texts read them. A format change bumps `SCHEMA` and teaches `migrate` the step.
- The tests fail when a backticked `tracker <command> --flag` in the docs, the skill, the templates or the code names a command or flag the CLI lacks: change the parser and the text together.
- Hook output and the brief enter every tracked session's context. Add a line only when it changes what the model does next, keep the brief within the `BRIEF_*` limits in `views.py`, and phrase requests as the action to take.
