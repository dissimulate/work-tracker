#!/bin/sh
# Hook entry point: hooks/hooks.json runs `hook.sh <event>` with the hook JSON on stdin. Each event is filtered here
# with shell built-ins, so Python (about 40 ms) starts only when it can add something. The filters only rule events
# out; Python decides what matches:
# - a session with no tracker (no `tracker start`, no TRACKER): one file test, then exit. At its start, Python only
#   on a git branch (git's files are read, git does not run), to look for an open ticket and link it or offer the
#   link, or while a session that /clear ended hands its tracker over (.cleared.json);
# - edit: Python only for a tracker file edited by hand (the project's files: git shows that work, see `lag`);
# - post-bash: Python only when the command names git and commit or push, or gh and pr;
# - prompt: in any session, Python for the user's /work-tracker:watch, which gives the session the watch or ends it.
event=$1
input=$(cat)
home=${TRACKER_HOME:-$HOME/.claude/trackers}

# value: the first "<name>": "<value>" string in the hook JSON, up to its first quote (ids and paths hold none). The
# key is matched with its colon, so a value that equals a key name ("hook_event_name": "prompt") is not taken for it.
field() {
  value=
  case $input in *"\"$1\":"*) ;; *) return ;; esac
  rest=${input#*\"$1\":}
  rest=${rest#*\"}
  value=${rest%%\"*}
}

field session_id
case $value in *[!A-Za-z0-9._-]*) sid= ;; *) sid=$value ;; esac
# Claude's shell commands get their id and PATH through its env file. Other hosts get both as a command prefix in
# context (`command_context` in Python), else read their own session id (`session_id`) and take bin/tracker's path
# from the skill.
if [ "$event" = session-start ] && [ -n "$CLAUDE_ENV_FILE" ] && [ -n "$sid" ]; then
  echo "export TRACKER_SESSION=$sid" >> "$CLAUDE_ENV_FILE"
  bin=$(cd "${0%/*}/../bin" && pwd)
  case ":$PATH:" in *":$bin:"*) ;; *) printf 'export PATH="$PATH:%s"\n' "$bin" >> "$CLAUDE_ENV_FILE" ;; esac
fi
session="$home/.sessions/$sid"

# Succeeds when the cwd is on a git branch; Python then looks for a ticket on it. A Windows path (C:\\x in the JSON)
# becomes C:/x, which Git Bash reads.
on_branch() {
  field cwd; dir=$value
  while :; do case $dir in *'\\'*) dir=${dir%%'\\'*}/${dir#*'\\'} ;; *) break ;; esac; done
  case $dir in /*|[A-Za-z]:/*) ;; *) return 1 ;; esac
  while [ ! -e "$dir/.git" ]; do
    case $dir in */*) dir=${dir%/*} ;; *) return 1 ;; esac
  done
  gitdir=$dir/.git
  if [ -f "$gitdir" ]; then  # a linked worktree: "gitdir: <path>"
    read -r line < "$gitdir"; gitdir=${line#gitdir: }
    case $gitdir in /*|[A-Za-z]:/*) ;; *) gitdir=$dir/$gitdir ;; esac
  fi
  [ -f "$gitdir/HEAD" ] || return 1
  read -r head < "$gitdir/HEAD"
  case $head in "ref: refs/heads/"*) return 0 ;; *) return 1 ;; esac
}

watch=
if [ "$event" = prompt ]; then
  field prompt
  case $value in /work-tracker:watch*|/watch*|'$work-tracker:watch'*|'$watch'*) watch=1 ;; esac
fi

if [ -z "$watch" ] && [ -z "$TRACKER" ] && ! { [ -n "$sid" ] && [ -f "$session.json" ]; }; then
  [ "$event" = session-start ] || exit 0
  # A host without an env file needs the command prefix even outside a git branch.
  { [ -n "$PLUGIN_ROOT" ] && [ -z "$CLAUDE_ENV_FILE" ]; } || [ -f "$home/.cleared.json" ] || on_branch || exit 0
fi

case $event in
  edit)
    [ -n "$sid" ] && [ -f "$session.json" ] || exit 0
    field file_path  # a tracker file: Python counts the change as this session's. On Windows, Python decides
    case $value in "$home"/*|[A-Za-z]:*) ;; *)
      # apply_patch has no file_path: its paths are in its command, absolute or relative to a cwd in $home.
      case $input in *"$home"*) ;; *) exit 0 ;; esac
      ;;
    esac
    ;;
  post-bash)
    case $input in *git*commit*|*git*push*|*gh*pr*) ;; *) exit 0 ;; esac
    ;;
esac
# Only the hooks' modules load (not the CLI or the viewer); Python reuses their compiled bytecode.
. "${0%/*}/python.sh"
printf '%s' "$input" | "$python" -I -X utf8 -c \
  'import sys; sys.path.insert(0, sys.argv.pop(1)); from tracker.hooks import run_hook; run_hook(sys.argv[1])' \
  "${0%/*}" "$event"
