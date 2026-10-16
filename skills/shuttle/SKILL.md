---
name: shuttle
description: Shuttle a coding task to cheaper agent CLIs (opencode free -> agy) routed by difficulty, then review lean and land it
argument-hint: "[easy|normal|hard|<cli>] <task description>"
---

Act as the director using the `orchestrate` skill (load it first). Spend as few of your own tokens as possible.

Request: $ARGUMENTS

1. If the first word is `easy`, `normal` or `hard`, use it as the difficulty; if it names a CLI (`opencode`, `agy`,
   `codex`, `grok`, `claude`), force that worker. Otherwise judge the difficulty yourself (`orchestrate` section 2;
   `shuttle_ratings` if unsure).
2. Find the exact files cheaply and write a precise brief (Goal / Files / Do / Don't / Done when) and an acceptance
   `check` command. Ask the operator only if an ambiguity would change the result.
3. Start the job(s) on the current repository, tell the operator the job ids and dashboard link in one line, wait
   with `shuttle_wait`, then review in tiers (machine verdict -> shape -> worker second opinion -> targeted read).
4. Rework or land; ask before landing unless the operator already said to land.
