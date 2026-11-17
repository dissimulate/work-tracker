# Sourced by bin/tracker and scripts/hook.sh before they start Python: sets $python. They start it with `-I`, so a
# module in the cwd (the user's project) cannot replace a stdlib one, and `-X utf8` (`-I` ignores PYTHONUTF8), because
# the tracker's files and output are UTF-8 and Windows' default encoding is not. On Windows `python3` can be the
# Microsoft Store stub, which starts no Python, so $python is the first of python3, python and py that runs Python 3.9+.
#
# Also sets $pycache, which they pass as `-X pycache_prefix`: the folder where Python keeps the modules' compiled
# bytecode. With `-I`, Apple's Python (the macOS system one) reads no HOME, so it finds no folder for its bytecode and
# compiles every module again at each start, the stdlib's too (about 100 ms). Empty on Windows, where Python keeps the
# bytecode beside the source and a Git Bash path is not a Windows path, and for a TRACKER_HOME that is not an absolute
# path, which would make the folder in the cwd. `store.prune_bytecode` removes what a plugin version that is gone left.
python=python3
pycache=
if [ "$OS" = Windows_NT ]; then
  for python in python3 python py; do
    "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null && break
  done
else
  pycache=${TRACKER_HOME:-$HOME/.claude/trackers}/.pycache
  case $pycache in /*) ;; *) pycache= ;; esac
fi
