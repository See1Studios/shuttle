---
name: shuttles
description: Show shuttle jobs and act on them (review, rework, land, discard)
argument-hint: "[job-id] [review|merge|discard|followup <feedback>]"
---

Use the `orchestrate` skill and the `shuttle_*` tools.

Arguments: $ARGUMENTS

- No arguments: call `shuttle_list` and show a compact table (id, cli, title, status, round). For jobs that are
  `done` and not yet reviewed, offer to review them.
- `<id>` alone or `<id> review`: fetch `shuttle_result`, review it in tiers per the `orchestrate` checklist, and
  give a verdict with concrete findings.
- `<id> followup <feedback>`: send the feedback as a rework round.
- `<id> merge`: confirm the job was reviewed in this session (review it first if not), then land it with
  `shuttle_merge` (squash) and a proper commit message.
- `<id> discard`: discard it.
