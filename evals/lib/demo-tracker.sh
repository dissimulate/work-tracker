# The workspace every case starts from, sourced by each case's scaffold.sh. A scaffold runs before the agent, outside
# its sandbox, in the empty workspace and with the run's temporary HOME. It makes a git repo on DEMO-2's branch and a
# `demo` tracker: DEMO-1 done, DEMO-2 in progress with its branch marked up to date, DEMO-3 waiting on it. The
# sandbox lets the agent's shell write only in the workspace, so the tracker lives in .trackers/ there, linked from
# ~/.claude/trackers (the default TRACKER_HOME), and git leaves it out. Graders read the tracker's files there.
set -e
root=$(cd "$(dirname "$0")/../.." && pwd)
tracker() { "$root/bin/tracker" --tracker demo "$@" > /dev/null; }

mkdir -p .trackers "$HOME/.claude"
ln -s "$PWD/.trackers" "$HOME/.claude/trackers"
git init -q -b feat/DEMO-2-users-api
git config user.email dev@example.com
git config user.name Dev
echo .trackers/ >> .git/info/exclude
printf 'from flask import Flask\n\napp = Flask(__name__)\n' > app.py
git add -A
git commit -qm "Start the users service"

"$root/bin/tracker" init demo --title "Users service" --owner dev > /dev/null
tracker add tracker context "- Spec: [Users service spec](https://example.com/users-spec) — the API contract"
tracker new DEMO-1 --title "Users table"
tracker new DEMO-2 --title "Users API" --depends DEMO-1 --branch feat/DEMO-2-users-api
tracker new DEMO-3 --title "Users admin page" --depends DEMO-2
tracker add DEMO-1 plan "Create the users table: id, email, created_at."
tracker add DEMO-1 carry "users.id is a UUID, not an int: whatever references a user must use UUID"
tracker add DEMO-1 carry "email unique"
tracker step DEMO-1 "Users table and migration merged" --done "the users table and its migration"
tracker add DEMO-2 plan "GET /users and GET /users/<id>, behind auth."
tracker set DEMO-2 status=in-progress "next=Build GET /users"
tracker synced
