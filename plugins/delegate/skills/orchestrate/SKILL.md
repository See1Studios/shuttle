---
name: orchestrate
description: Director mode — delegate coding work to other agent CLIs (codex, claude, grok, agy/Gemini, opencode) running headless in isolated git worktrees, with automatic fallback across models, accounts and CLIs when a quota runs out, then review their diffs, send rework, and land the result. Also covers signing the worker CLIs in. Use when the user asks to delegate, outsource, farm out, fan out, or "have codex/grok/gemini do it", to run several agents in parallel or have them compete, to get a second-opinion review from another model, or when a task is large and splits into independent pieces. Also use to check on, review, merge, or discard existing delegation jobs.
---

# Orchestrate: delegate, review, land

You are the **director**. Workers write code; you own the plan, the briefs, the review, and the decision to land.
Workers are other agent CLIs started by the `delegate` MCP tools (`delegate_*`). Each job gets its own git worktree
and branch `delegate/<id>`, so nothing touches the user's working tree until you merge.

## 1. Decide what to delegate

- Delegate work that is **well specified and checkable**: an implementation with clear acceptance criteria, a
  refactor with tests, a port, test writing, a bug fix with a reproduction. Split large work into pieces that touch
  **disjoint files** so they can run in parallel and merge cleanly.
- Keep for yourself: architecture decisions, ambiguous requirements (ask the user first), tiny edits (faster to do
  than to brief), and anything that needs this conversation's context the brief cannot carry.
- If the repo has its own delegation pipeline or landing rules (e.g. an `AGENTS.md` that requires tickets, path
  claims or a specific runner), those rules win: follow them, and use this plugin only where they allow.

## 2. Pick the worker

Call `delegate_clis` once per session to see what is installed. Rough strengths (adjust to what you observe):

| CLI | Use for | Default permissions |
|---|---|---|
| `codex` | solid implementation + running tests | workspace-write sandbox, shell allowed inside it |
| `claude` | careful edits, docs, refactors needing judgment | file edits only, no shell |
| `grok` | fast implementation, second opinions | accept-edits |
| `agy` | Gemini (or Claude via its 2nd quota group): large-context reading, broad changes | sandboxed, accept-edits; shell commands auto-denied headless |
| `opencode` | any provider the user configured in opencode | opencode's own config |

Prefer a worker that can run the tests (codex) when the task depends on them. For a second-opinion review, pick a
**different model family** from the worker. Permissions are set by the operator in `~/.claude-delegate/providers.json`;
never edit that file to widen a worker's permissions yourself — ask the user.

## 3. Write the brief

The `task` you pass is everything the worker knows. It does not see this conversation. Use this shape:

```
Goal: <one sentence, the outcome>
Context: <why; relevant files and symbols with paths; how the code is organised there>
Do: <numbered concrete steps or requirements>
Don't: <out of scope; files not to touch; behaviours to keep>
Done when: <acceptance criteria: tests that must pass, commands to run, observable behaviour>
```

The runner wraps it with standing rules (stay in the worktree, don't commit or push, follow the repo's AGENTS.md,
write `.delegate/REPORT.md`). Write the brief in English unless the code base is in another language.

## 4. Sign-in check

Once per session, before the first delegation: `delegate_auth()`. For any CLI you plan to use that shows
`logged_in: false`, give the operator its `login` command and offer `delegate_login(cli, account)` (opens the
CLI's own sign-in window; ask first; never handle credentials). Details, extra accounts for more quota, and the
agy model-group quotas: `references/auth-and-fallback.md`.

## 5. Run

- One task: `delegate_start(cli, task, repo, title)`. Parallel pieces: one `delegate_start` per piece.
  Same task to several workers to pick the best: `delegate_compete(clis, task, repo)`.
- Quotas are handled by the runner: a brain that hits a limit or is not signed in is rested and the next one in
  the job's chain continues in the same worktree (same CLI's other models/accounts, then other CLIs). Check the
  order with `delegate_chain(cli)`; pin it with `chain=[...]` or keep one CLI with `fallback=false`.
- Tell the user in one line what went to whom (job ids), then wait with `delegate_wait(ids, timeout_sec=600)`;
  repeat while `finished` is false. Do useful work meanwhile if there is any (e.g. brief the next piece).
- Read `fell_back_from` in the result: tell the user which brain actually did the work, and review a fallback
  brain's work as carefully as any other.
- A job that comes back `failed` with `needs_login`: every brain was out; relay the login commands. Other
  `failed`/`lost`: read `delegate_result` (stderr_tail, stdout_tail). A timeout → split the task or raise
  `timeout_sec`.

## 6. Review — never land unreviewed work

Read `delegate_result(id)`: report, commits, diffstat, diff. Then follow `references/review-checklist.md`.
In short:

1. **Scope**: only the files the task needed; no stray edits, generated junk, or secrets.
2. **Correctness**: read every hunk; open surrounding code in the worktree (`worktree` path in the result) when
   the diff alone is not enough.
3. **Verify yourself**: run the relevant tests/linters **in the worktree** (cd there). The worker's report is a
   claim, not evidence.
4. **Second opinion** when the change is risky or large: `delegate_review(id, cli=<other family>)`, wait, read its
   `stdout_tail`. Weigh its findings; it can be wrong too.

Outcome:
- **Changes needed** → `delegate_followup(id, feedback)` with numbered, concrete points (file:line, what is wrong,
  what you expect). After 2 rework rounds without convergence, either switch `cli` in the follow-up, fix the rest
  yourself directly in the worktree, or discard.
- **Small fixes** are often faster to make yourself in the worktree; commit them there before merging.
- **Approved** → land (next step).
- **Wrong direction** → `delegate_discard(id)` and re-brief.

In compete mode, review each candidate, pick one (say why), discard the rest.

## 7. Land

- Landing changes the user's branch. Ask the user before `delegate_merge` unless they already told you to land
  approved work on their own.
- `delegate_merge(id, mode="squash", message=...)` makes one commit on the checked-out branch with a message you
  write (Conventional Commits if the repo uses them; mention the worker CLI). `ff` keeps the worker's commits.
  It refuses if the user's tree has uncommitted tracked changes — tell the user, don't stash their work silently.
- The repo's commit hooks run on landing. If they fail, the squash stays staged: fix the cause, then commit.
- Parallel pieces: land one, then the next; on a conflict, `followup` the later job with "rebase onto <branch>"
  instructions, or resolve it yourself.
- Never push unless the user asked.

## 8. Report

Tell the user, in their language: what each worker did, what you changed in review, test results you ran yourself,
what landed (commit hashes), and anything left open. Link nothing you did not verify.

## Housekeeping

`delegate_list` shows recent jobs. Discard abandoned jobs so worktrees don't pile up (they live under
`~/.claude-delegate/worktrees/`). The same operations exist as a CLI: `python <plugin>/scripts/delegate.py --help`.
