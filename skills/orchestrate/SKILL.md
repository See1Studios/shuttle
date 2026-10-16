---
name: orchestrate
description: Director mode for saving tokens — shuttle implementation work to cheaper agent CLIs (opencode's free models first, then agy/Gemini, then agy's Claude group; codex, grok and the claude CLI only when enabled or named) running headless in isolated git worktrees, routed by task difficulty and by each model's track record, with an automatic test-retry-escalate loop, so you (the director) spend tokens only on the brief and the final judgment. Use when the user asks to shuttle, delegate, outsource, farm out, fan out, or "have opencode/agy/gemini do it", wants to save tokens or quota, wants parallel or competing agents, or a second-opinion review from another model; also to check, review, merge or discard shuttle jobs, read worker ratings, sign workers in, or swap agy's Google account.
---

# Orchestrate: spend the cheapest tokens that can do the job

Cost order on this host: **opencode free models < agy (paid, plentiful) < director session (scarce)**. You (the
director — whether running in Claude Code, Codex, Antigravity, Grok, or OpenCode) write the brief and make the final
call; writing code, running tests, retrying and first-pass review belong to the workers and the runner. Every tool
result you read costs tokens: ask for the smallest answer that lets you decide.

Tools are `shuttle_*` (MCP). Each job gets its own git worktree and branch `shuttle/<id>`; nothing touches the
operator's tree until you merge.

## 1. Shuttle by default

- Shuttle every task that is **specified and checkable**: features with acceptance tests, bug fixes with a
  reproduction, refactors under tests, ports, test writing, docs, plumbing. Split big work into pieces touching
  **disjoint files** and run them in parallel — small pieces also let the free tier take more of them.
- Keep only: design decisions, ambiguity you must settle with the operator, and edits shorter than their brief.
- The repo's own rules (an `AGENTS.md` requiring tickets, path claims, its own runner) win; follow them.

## 2. Route by difficulty — the runner picks the worker

Leave `cli` empty and set `difficulty`; the tier decides who starts and who takes over:

| difficulty | use for | chain (cheapest first) |
|---|---|---|
| `easy` | mechanical edits, renames, docs, simple tests, single-file changes with a clear check | opencode free → agy Gemini Flash low → medium → high |
| `normal` (default) | typical feature/bug work in a few files with tests | opencode free → Flash high → Sonnet → Opus → Argon (medium) |
| `hard` | cross-module design, subtle concurrency/security, unclear failure modes | Argon → Opus → Sonnet (high) → Flash high |

Models and effort are picked from the live `agy models` / `opencode models` lists (`shuttle_chain(difficulty=...)` shows the
exact rungs); a new model joins by itself. Stuck workers (no progress for 4 min) are stopped and passed on.

When a worker cannot make `check` pass after its automatic fix rounds, or fails outright, the next brain takes over
the same worktree — no tokens of yours spent. Quota, sign-in and permission stops fall through the same chain.
`shuttle_chain(difficulty)` shows the current chain and rests.

**Ratings steer it.** Every outcome is scored per model and difficulty (`shuttle_ratings`): passed (fewer
automatic rounds = higher), escalated, failed, and your verdicts — accepted (merge), rework (followup), rejected
(discard). A model proven weak at a difficulty is skipped there (with occasional re-tries so it can recover). So:
rate honestly — merge what you accept, `discard` what you reject — and check `shuttle_ratings` now and then to
re-label work: if opencode passes most `normal` jobs, mark more of your tasks `easy`/`normal`; if it keeps
escalating on a kind of task, call that kind `hard` from the start.

Name `cli` only to force a worker. `claude` spends Claude quota and is never automatic. `codex` and `grok` are
disabled on this host (no subscription; remove them from `disabled` in `~/.shuttle/brains.json` to use).

## 3. The brief — where your tokens buy the most

Weaker workers need precision, not volume. Find the exact files and symbols cheaply (code index, not whole-file
reads), then write:

```
Goal: <one sentence>
Files: <exact paths to change; paths to read for context; nothing else>
Do: <numbered, concrete steps; name functions, signatures, data shapes>
Don't: <files/behaviour to leave alone>
Done when: <the check command passes; observable behaviour>
```

**Always pass `check`**: the acceptance command the runner runs after each round (e.g.
`py -3 -m pytest -q tests/test_x.py`, a build, a lint). A testable task without a `check` costs you a review the
machine could have done, and gives the ratings nothing to learn from. No test yet? Make writing one step 1 and point
the check at it. `auto_fix_rounds` (default 2) is per worker before it escalates.

## 4. Run and wait cheaply

- `shuttle_start(task, repo, difficulty=..., check=..., title=...)`; one call per parallel piece.
  `shuttle_compete` only when the choice really matters: it doubles what you review.
- One line to the operator with the job ids and the `dashboard` link from the start result (the job brings the
  dashboard up itself; in desktop apps with a browser pane you can also open that link beside the chat),
  then `shuttle_wait(ids, timeout_sec=600)` (compact states). Don't
  poll `shuttle_status` in between; don't narrate waits.
- Once per session before the first job, or when a job reports `needs_login` / `phase: waiting_login`:
  `shuttle_auth()` — it opens the sign-in window of each worker missing one; tell the operator to finish it there
  (the waiting job carries on by itself). `shuttle_dashboard()` when they want to see and manage it all. See
  `references/auth-and-fallback.md`. opencode needs no sign-in: never ask the operator to log it in.
- A `warnings` entry about the main working tree changing while a worker ran means a possible escaped write:
  look at the operator's `git status` before anything else and tell them.

## 5. Review in tiers — stop at the first tier that decides

1. **Machine verdict** (free): `check: passed` / `failed …`, `escalated`. Still failing at the end of the chain →
   read `check_result.tail` via `shuttle_result`; then one targeted `shuttle_followup`, or re-label as `hard`
   and restart, or split the task.
2. **Shape** (cheap): `shuttle_result(id)` = report + diffstat, *no diff*. Touched files match the brief's
   `Files:`? Strays → followup "revert X" without reading further.
3. **Second opinion** (worker tokens): for non-trivial diffs, `shuttle_review(id, cli="agy")` or
   `cli="opencode"`; read only the `VERDICT` line and blocker/major findings.
4. **Targeted read** (your tokens, last): `shuttle_result(id, paths=[...])` for only the risky files (public
   interfaces, security, concurrency, data formats). Never pull the whole diff of a large change.

`references/review-checklist.md` lists what to look for in tier 4. Fix tiny things yourself in the worktree only
when cheaper than a followup; otherwise followup with numbered file:line points.

## 6. Land

- Ask the operator before `shuttle_merge` unless they told you to land approved work on their own.
- `shuttle_merge(id)` (squash) applies the worktree's change to the checked-out branch and commits once; the repo's
  commit hooks run **once, there** (rounds are staged, not committed). It refuses while the operator's tree has
  uncommitted tracked changes. A refusing hook leaves the change staged: report it, then fix or followup.
- Commit message: what and why, Conventional Commits if the repo uses them, naming the worker that did it.
- Merge = "accepted" and discard of finished work = "rejected" in the ratings. Never push unless asked.

## 7. Report

Briefly, in the operator's language: which brain actually did each piece (`escalated`, `fell_back_from`), check
results, what you verified, what landed (hash), what is left.

## Housekeeping

`shuttle_list` shows recent jobs; discard abandoned ones. CLI equivalent: `python <plugin>/scripts/shuttle.py --help`.
