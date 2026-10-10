#!/bin/sh
# Runs the model evals with this repo's settings. README "Evals" says when to run them and what each tag covers.
#
#   evals/run.sh quick <tag>...      the cases with any of the tags, once each, on Sonnet
#   evals/run.sh quick all           every case, once each, on Sonnet
#   evals/run.sh release             every case, its own run count (3 unless its prompt.md sets `runs`), on Opus
#
# Options after the mode's words go to `claude plugin eval` as they are, e.g. `--case <name>` or `--keep-temp`.
#
# What each fixed option does:
#   --scaffold        each case's scaffold.sh makes a git repo and a `demo` tracker in the run's workspace
#   --allow-tools     the agent needs Bash to run the CLI
#   --ablation none   with no plugin there is no tracker, so a baseline run measures nothing
#   -j 8              8 sessions at a time, sharing your rate limit: a suite of 8 runs or fewer takes about as long
#                     as its slowest run
#   --model           a run reads none of your settings, so without it the model is the built-in default
#
# Effort: `plugin eval` has no option for it, and a case's env takes only EVAL_* variables. Sonnet's default effort is
# medium. CLAUDE_CODE_EFFORT_LEVEL is set too, though the docs do not say whether it reaches the run; no result records
# the effort used.
set -eu
cd "$(dirname "$0")/.."

usage() {
  sed -n '4,6p' "$0" | sed 's/^# *//' >&2
  exit 2
}

mode=${1:-}
[ $# -gt 0 ] && shift
case $mode in
quick)
  tags=
  while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do
    tags="$tags $1"
    shift
  done
  [ -n "$tags" ] || usage
  # shellcheck disable=SC2086  # one word per tag
  [ "$tags" = " all" ] && set -- --runs 1 "$@" || set -- --tag $tags --runs 1 "$@"
  model=sonnet effort=medium
  ;;
release)
  model=opus effort=high
  ;;
*)
  usage
  ;;
esac

CLAUDE_CODE_EFFORT_LEVEL=$effort exec claude plugin eval . --scaffold --allow-tools Bash --ablation none -j 8 \
  --model "$model" "$@"
