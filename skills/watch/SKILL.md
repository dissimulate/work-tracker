---
name: watch
description: Watch a tracker - get each change to its tickets, agents and PRs as it happens, and act on it as told. Only when the user, or a skill or instruction from them, tells you to watch a tracker; never on your own. Give a tracker name and the task, or stop.
argument-hint: "<tracker name> [what to do] | stop"
---

# Watch a tracker

This session watches a tracker: it gets each change to the tracker's tickets, agents and PRs as it happens, and acts on it as the user, or the skill that started the watch, asked. `$ARGUMENTS` gives the tracker's name, then the task. With no task in the arguments or the conversation, ask the user what to watch for.

Use the command prefix from the session's `[work-tracker]` hook when supplied. If `tracker` is absent from PATH, use the absolute path to `bin/tracker` at this plugin's root (two directories above this file).

**`stop`**: when the arguments are `stop`, or the user or the task says to stop, run `tracker watch --stop` (a running `tracker watch` of this session stops by itself), say so in one line, and end the turn.

## Run the watch

1. Start `tracker watch <tracker> --hold 60`. Its first lines are the state now; each later batch is what changed. With `--hold 60` the lines that need no one come at most once a minute, and a `!` line at once. Add `--only <kinds>` to keep only what the task needs (`tracker watch --help` lists the kinds). A session asked to coordinate the tracker starts it as `tracker watch <tracker> --hold 60 --as coordinator`: decisions with no owner then come to it before a human, and on its watch `!` also marks a question another session raised that is its own to triage.
   - When the host has a tool that turns each line a background command prints into an event (in Claude Code: Monitor), run the command with it, at its longest timeout. When the timeout stops the command, start it again at once: it goes on from where it stopped, and misses nothing.
   - Else add `--once` and run the command with the host's background command tool; if that returns a process handle, wait for its output with the host's process tool. It exits after one batch: start it again after each one.
   - Exit 3 lists trackers: ask the user which one. Exit 4: the watch has ended (`--stop`), or this session watches the tracker already. Say why in one line, and stop.
2. Each line of a batch starts with the time, then what it is about: a ticket or record id, `agent <name>`, `check` or `github`. `!` marks what needs a human: a move that became theirs (a review came back, checks fail, ready to merge, a question only a human can answer), an agent that asks them for a permission or an answer, an agent idle and waiting on them, a new `check` error, a failing GitHub sync.
3. Act on each batch as the task says.

## What you can use

- Read: `tracker context <id> --brief`, `tracker show <id> --section <name>`, `tracker index --active`, `tracker decisions --all`, `gh pr view <n>`, `git log`.
- `tracker agents <tracker>`: each agent's status, tickets, branch and folder, its Claude Code session name and Herdr terminal when it has them, and its transcript file. To see what an agent did, read the end of its transcript (it is large: never all of it).
- Write: the tracker's commands work here as in any session, under the same rules (the tracker skill, `tracker rules`). Your own writes come back in the next batch.
- The watch does not put this session on the tracker. On no tracker, or on another one, give `--tracker <slug>` to each command; `tracker start <slug>` puts the session on it, with the brief.
- Tell the user: the host's notification tool when available, else a reply; one line under 200 characters that leads with the action.
