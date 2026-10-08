#!/bin/sh
. "$(dirname "$0")/../lib/demo-tracker.sh"
# Three finished tickets with exact times: wait times 24 h, 3.0 d and 5.0 d (median 3.0 d), cycle times 6 h, 2.0 d
# and 12 h (median 12 h).
for n in 4 5 6; do
  tracker new "DEMO-$n" --title "Shipped part $n"
  tracker add "DEMO-$n" link "Issue: [SC-$n Story $n](https://issues.example.com/story/$n)"
  tracker set "DEMO-$n" status=done "summary=part $n shipped"
done
tracker issue DEMO-4 --created 2026-08-31T00:00:00Z
tracker issue DEMO-5 --created 2026-08-30T00:00:00Z
tracker issue DEMO-6 --created 2026-08-29T00:00:00Z
# The starts as `set status=in-progress` records them, and the merges as `sync` records them from GitHub's PRs.
python3 - "$root/scripts" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from tracker import github, model
tr = model.tracker_at("demo")
def pr(n, merged):
    return ("acme/users", {"number": n, "state": "MERGED", "isDraft": False, "mergedAt": merged, "baseRefName": "main"})
with model.locked():
    for ident, started in (("DEMO-4", "2026-09-01T00:00:00Z"), ("DEMO-5", "2026-09-02T00:00:00Z"),
                           ("DEMO-6", "2026-09-03T00:00:00Z")):
        tr.lookup(ident).save({"started_at": started})
    github.apply_sync(model.tracker_at("demo"), {"DEMO-4": pr(14, "2026-09-01T06:00:00Z"),
                                                 "DEMO-5": pr(15, "2026-09-04T00:00:00Z"),
                                                 "DEMO-6": pr(16, "2026-09-03T12:00:00Z")})
PY
