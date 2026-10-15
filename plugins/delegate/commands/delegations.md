---
description: Show delegation jobs and act on them (review, rework, land, discard)
argument-hint: "[job-id] [review|merge|discard|followup <feedback>]"
---

Use the `orchestrate` skill and the `delegate_*` tools.

Arguments: $ARGUMENTS

- No arguments: call `delegate_list` and show a compact table (id, cli, title, status, round). For jobs that are
  `done` and not yet reviewed, offer to review them.
- `<id>` alone or `<id> review`: fetch `delegate_result`, review it per the checklist (run the tests in the
  worktree), and give a verdict with concrete findings.
- `<id> followup <feedback>`: send the feedback as a rework round.
- `<id> merge`: confirm the job was reviewed in this session (review it first if not), then land it with
  `delegate_merge` (squash) and a proper commit message.
- `<id> discard`: discard it.
