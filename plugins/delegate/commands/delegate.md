---
description: Delegate a task to another agent CLI (codex, claude, grok, agy, opencode) in an isolated worktree, then review and land it
argument-hint: "[cli|cli1,cli2|auto] <task description>"
---

Act as the director using the `orchestrate` skill (load it first).

Request: $ARGUMENTS

1. If the first word names a CLI (or a comma-separated list for compete mode), use it; if it is `auto` or absent,
   choose per the skill's table after `delegate_clis`.
2. Inspect the repository enough to write a complete brief (goal, context with paths, do, don't, done when).
   If the request is ambiguous in a way that changes the result, ask the user before starting.
3. Start the job(s) on the current repository, tell the user the job ids in one line, wait, then review per the
   skill's checklist (run the tests yourself in the worktree).
4. Send rework if needed. When it is ready, ask the user before landing unless they already said to land.
