---
description: A whole section is replaced through the CLI (`put`, or `drop` and `add`), with backticks intact, not by editing the file.
runs: 1  # one command the CLI makes plain; 3/3 in each of 5 full runs
tags: [write]
max_turns: 25
timeout_seconds: 600
allowed_tools: [Bash, Read, Glob, Grep, Skill]
---

In the demo tracker, DEMO-1 is done but its Carry forward is a mess. Replace the whole Carry forward with exactly these three bullets, word for word, and nothing else:

- `users.id` is a UUID; every table that references a user uses it.
- `users.email` is unique and stored lower-case.
- Migrations live in `db/migrations/` and run in file-name order.
