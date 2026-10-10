#!/bin/sh
. "$(dirname "$0")/../lib/demo-tracker.sh"
# Work committed in an earlier session on the tracker and logged as it would be: `tracker start` logs the branch's new
# commits, as the hooks do. It runs here, outside the sandbox: in the sandbox the CLI may not run git (README "Evals"),
# so the case's session may not log it itself.
printf '\n\n@app.get("/users")\ndef users():\n    return []\n' >> app.py
git commit -qam "Add GET /users"
TRACKER_SESSION=earlier "$root/bin/tracker" start demo > /dev/null
printf '{"session_id": "earlier", "cwd": "%s"}' "$PWD" | "$root/scripts/hook.sh" stop
