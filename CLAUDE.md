# work-tracker plugin

- Verify with `python3 -m unittest discover tests` (about 10 s, no network, temporary git repos) and `uvx ruff check`.
- `claude plugin eval . --scaffold --allow-tools Bash --ablation none` runs the model evals in `evals/` (README "Evals"): each run is a full Claude session on the user's plan. After a change to the skill, the brief or the hook texts, run only the cases that text affects, once: add `--case <glob> --runs 1`. Run the whole suite (3 runs per case) only before a release, and not as a routine check.
- Ship a change by bumping `version` in `.claude-plugin/plugin.json` (installed copies are cached by version), then push. A marketplace that pins this plugin to a commit (`sha`) gets the release only through a PR there that moves the pin to the pushed commit: after the push, ask the user whether to open one.
- Code layout and module order: README "Code". A module imports only the modules before it in that order.
- Contract constants in `scripts/tracker/model.py` are the single source; `check`, `rules`, `set`, the templates and the hook texts read them. A format change bumps `SCHEMA` and teaches `migrate` the step.
- The tests fail when a backticked `tracker <command> --flag` in the docs, the skill, the templates or the code names a command or flag the CLI lacks: change the parser and the text together.
- Hook output and the brief enter every tracked session's context. Add a line only when it changes what the model does next, keep the brief within the `BRIEF_*` limits in `views.py`, and phrase requests as the action to take.
