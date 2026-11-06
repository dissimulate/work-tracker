---
name: watch
description: Make this session a read-only overseer of a tracker that tells you what needs you. Give a tracker name, or stop.
argument-hint: "<tracker name> | stop"
disable-model-invocation: true
---

# Watch a tracker

The user made this session the **overseer** of a tracker (`$ARGUMENTS`): like a project manager, you watch the work other sessions do and bring the user what needs them. You watch only: the other sessions do the work and record it.

Use the command prefix from the session's `[work-tracker]` hook when supplied. If `tracker` is absent from PATH, use the absolute path to `bin/tracker` at this plugin's root (two directories above this file).

**`stop`**: when the arguments are `stop`, the watch has ended (the hook removed this session's grant; a `tracker watch` still running exits by itself). Say so in one line and end the turn.

## Loop

1. Run `tracker watch $ARGUMENTS --once` with the host's background command tool. If it returns a process handle, wait for its output with the host's process tool. The command waits until something changes, prints one line per ticket, agent or check, then exits. The first run prints the state now. Exit 3 lists trackers: ask the user which one.
2. Read the batch. Each line starts with the time; `!` marks what needs the user: a move that became theirs (a review came back, checks fail, ready to merge), an agent that asks them for a permission or an answer, an agent idle and waiting on them, a new `check` error, a failing GitHub sync.
3. Judge the batch as a whole, then act:
   - the user must act now: one notification under 200 characters that leads with the action, such as "S-3: changes requested by rev; agent api idle 12 min, waits on you". Use the host's notification tool when available; otherwise reply in chat.
   - worth knowing, not urgent: one short line in your reply
   - routine progress: no note
4. Go back to step 1 at once. The loop ends only when the user says stop, or when `tracker watch` exits with code 4 (the user ended the watch, or another watcher has the tracker): then say why in one line.

## Look closer

When a line hints at trouble, read before you judge: `tracker context <id> --brief`, `tracker show <id> --section <name>`, `tracker index --active`, `tracker decisions --all`, `gh pr view <n>`, `git log`. Bring the user:

- a step or plan that contradicts a settled decision
- a stuck agent: busy for long with nothing recorded, or the same step again and again
- a changed Carry forward that a ticket under way builds on
- work outside a ticket's Scope or Plan
- two agents on one ticket or branch

## Watch only

- Your channel is the user: notifications and replies. The CLI refuses every tracker write in this session, and the agents record their own work.
- This session stays on no tracker: pass by any `[work-tracker]` offer to link it.
- Replies stay short; the notifications carry the news. When the user asks for a summary, give what moved, what waits on them and what looks wrong.
