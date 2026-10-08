# Sourced by bin/tracker and scripts/hook.sh before they start Python: sets $python. They start it with `-I`, so a
# module in the cwd (the user's project) cannot replace a stdlib one, and `-X utf8` (`-I` ignores PYTHONUTF8), because
# the tracker's files and output are UTF-8 and Windows' default encoding is not. On Windows `python3` can be the
# Microsoft Store stub, which starts no Python, so $python is the first of python3, python and py that runs Python 3.9+.
python=python3
if [ "$OS" = Windows_NT ]; then
  for python in python3 python py; do
    "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null && break
  done
fi
