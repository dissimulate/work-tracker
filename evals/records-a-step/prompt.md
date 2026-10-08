---
description: A step finished before the session - its commit is logged once, by the hooks or `step`, not again in a message, no log line repeats the push or the test run, and the new `next` is recorded with `tracker step`.
max_turns: 25
timeout_seconds: 600
allowed_tools: [Bash, Read, Glob, Grep, Skill]
---

I'm continuing DEMO-2 in the demo tracker. Before this session I committed the GET /users route (it's the last commit) and pushed it, and the tests passed: that finishes the step. The next step is the auth middleware for /users. Bring the tracker up to date; don't write any code.
