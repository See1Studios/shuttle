# claude-delegate

A Claude Code plugin that turns Claude into a **director that spends as few of its own tokens as possible**. Claude
writes a brief and an acceptance check; cheaper agent CLIs do the work headless in isolated git worktrees, routed by
difficulty and by their track record; a runner tests, retries and escalates without Claude; Claude reads only the
verdict and what is risky, then lands the result. Modelled on PrivateEngine's worktree delegation
(`engine/dev/worktree_runner.py`, `tools/brain_limits.py`, `providers/account_login.py`), without its ticket governance.

```
Claude: brief + check ──> runner ──> opencode (free) ──check fails──> agy Gemini ──> agy Claude group
                                        │  quota / not signed in / blocked -> next brain, same worktree
          verdict, diffstat, report <───┘  ratings.jsonl learns who passes what -> routing skips proven-weak brains
Claude: tiered review (machine -> shape -> worker 2nd opinion -> targeted read) -> merge (one hooked commit) | rework | discard
```

## Parts

| Part | Path | What it does |
|---|---|---|
| Runner + CLI | `plugins/delegate/scripts/delegate.py` | worktrees, background jobs, briefs, check/auto-fix/escalate loop, lean results, rework, second-opinion reviews, merge/discard |
| Brains | `plugins/delegate/scripts/brains.py` | difficulty tiers, fallback chains, quota-group rests, sign-in checks, accounts |
| Ratings | `plugins/delegate/scripts/ratings.py` | per model x difficulty scores from outcomes and director verdicts; routing that skips proven-weak brains |
| agy account swap | `plugins/delegate/scripts/agy_login.py` | `/logout` -> Google OAuth link -> pasted code, in a pseudo console |
| MCP server | `plugins/delegate/scripts/mcp_server.py` | the same operations as `delegate_*` tools |
| Skill | `plugins/delegate/skills/orchestrate/` | the token-saving director procedure, review checklist, sign-in & fallback guide |
| Commands | `plugins/delegate/commands/` | `/delegate`, `/delegations`, `/delegate-auth` |

## Install

Requirements: Python 3.10+, `pip install mcp pywinpty` (mcp 1.x or 2.x; pywinpty only for the agy account swap),
git, and the worker CLIs on `PATH`.

```bash
claude plugin marketplace add C:/Users/user/Documents/GitHub/claude-delegate
```

```bash
claude plugin install delegate@claude-delegate
```

After changing files here: `claude plugin update delegate@claude-delegate`, then start a new session.

## Use

- `/delegate normal add retry with backoff to the HTTP client` — Claude writes the brief and check, routes by
  difficulty, reviews in tiers, asks before landing.
- `/delegations` — jobs; `/delegations <id> review|merge|discard|followup <feedback>`.
- `/delegate-auth` — sign-in state; opens sign-in where missing; agy account swap.
- Or just ask: "이건 opencode/agy한테 맡기고 결과만 검토해줘".

CLI without Claude: `python plugins/delegate/scripts/delegate.py --help` — `start --difficulty normal --check "..."`,
`wait`, `result [--paths a,b]`, `check`, `followup`, `review`, `merge`, `discard`, `chain`, `ratings`, `limits`,
`auth`, `login`, `agy-switch`.

## Routing, escalation, ratings

| difficulty | chain |
|---|---|
| easy | opencode -> agy Gemini medium -> agy Gemini high |
| normal (default) | opencode -> agy Gemini high -> agy Claude Sonnet |
| hard | agy Claude Sonnet -> agy Gemini high |

- With a `check`, the runner runs it after each round; on failure the same worker gets the output and retries
  (`auto_fix_rounds`, default 2), then the job **escalates** to the next brain in the same worktree.
- Quota (rest until the reset in the error), missing sign-in, permission blocks and crashes also move down the chain.
- `ratings.jsonl` records per model and difficulty: passed (minus a little per automatic round), escalated, failed,
  and the director's accepted (merge) / rework (followup) / rejected (discard). Score = Laplace-smoothed success
  rate. Routing skips a brain with >= 4 events and score < 0.35 at that difficulty, except 15% of the time
  (exploration). `delegate.py ratings` shows the table.
- codex and grok are implemented but **disabled** (`brains.json` `disabled`). The claude CLI is never automatic.

## Sign-in and accounts

opencode needs none (free models). agy has one Google login at a time on Windows: swap it with
`delegate.py agy-switch` (link + pasted code); its quota rests are kept per email. claude / codex / grok support
extra accounts through per-account config folders. Guide: `plugins/delegate/skills/orchestrate/references/auth-and-fallback.md`.

## Worker permissions (safety)

Defaults are each CLI's restricted headless mode; the plugin never widens them:

| CLI | Default command | Shell? |
|---|---|---|
| opencode | `run --standalone` (its own permission config) | per config |
| agy | `agy-real --sandbox --mode accept-edits -p` | no: headless shell is auto-denied, and agy stops at its first command (detected as `blocked`) |
| claude | `--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep -p` | no |
| codex | `exec -s workspace-write` | yes, sandboxed to the worktree |
| grok | `--permission-mode acceptEdits -p` | no |

To give agy what it needs, the operator either adds allow rules to agy's `settings.json` (`permissions.allow`, e.g.
`command(python)`, `command(git)`), or overrides its command in `~/.claude-delegate/providers.json`:

```json
{
  "agy": { "work": ["agy-real", "--sandbox", "--mode", "accept-edits", "-p"] },
  "mycli": { "work": ["mycli", "run"], "review": ["mycli", "run", "--read-only"], "model_flag": "--model",
             "author": ["MyCLI", "mycli@localhost"] }
}
```

Reviewers run read-only in an empty folder holding only the review brief. While a worker runs, the runner compares
the operator's `git status` before and after and puts any change outside the worktree into the job's `warnings`.

## Landing and hooks

Rounds are staged in the worktree, not committed, so the repo's commit hooks do not run per round. `merge`
(squash, default) applies the whole change to the checked-out branch and commits once, running the hooks once.
`ff` / `merge` modes commit the job branch first (hooks there) and then merge without a second hook run. Landing
refuses while the operator's tree has uncommitted tracked changes; workers never push.

## State

`~/.claude-delegate/` (or `$DELEGATE_HOME`): `jobs/<id>/` (job.json with attempts, checks, timings; task.md;
per-attempt logs), `worktrees/<id>/`, `accounts/`, `limits.json`, `ratings.jsonl`, `agy-room/`, optional
`brains.json` and `providers.json`. `/.delegate/` and tool caches are kept out of commits via `.git/info/exclude`.
