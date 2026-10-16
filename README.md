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
| Dashboard | `plugins/delegate/scripts/dashboard.py` | local page: sign-ins with a login button, rests, free models, chains, jobs |
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
- `python plugins/delegate/scripts/delegate.py dashboard` (or the `delegate_dashboard` tool) — management page at
  `http://127.0.0.1:8765/`: who is signed in (a 로그인 button opens that CLI's sign-in window), resting quota groups
  (clear them), opencode's free models, the difficulty chains, and the jobs (cancel / discard).
- Or just ask: "이건 opencode/agy한테 맡기고 결과만 검토해줘".

CLI without Claude: `python plugins/delegate/scripts/delegate.py --help` — `start --difficulty normal --check "..."`,
`wait`, `result [--paths a,b]`, `check`, `followup`, `review`, `merge`, `discard`, `chain`, `ratings`, `limits`,
`auth [--open]`, `login`, `agy-switch`, `dashboard`.

## Routing, escalation, ratings

| difficulty | chain (built from the live model lists) |
|---|---|
| easy | opencode free x3 -> agy Gemini Flash low -> medium -> high |
| normal (default) | opencode free x3 -> agy Gemini Flash high -> Claude Sonnet -> Claude Opus -> Gemini Argon (medium) |
| hard | agy Gemini Argon -> Claude Opus -> Claude Sonnet (high) -> Gemini Flash high |

The agy rungs come from `agy models` (re-read every 6 h, so a new model needs no edit), ranked smartest first by
`agy_rank` (argon > opus > sonnet > flash; newest version of each), with the effort variant by difficulty
(`effort`: easy low, normal medium, hard high). Quota pools: argon, gemini, claude-gpt. opencode's free models come
from `opencode models`, ordered by `opencode_prefer` (space-bunny first: fastest in a test-backed bake-off). A
`tiers` entry in `brains.json` still replaces a tier; claude / codex get the effort as a flag. The dashboard shows
the ranking and the chains.

- With a `check`, the runner runs it after each round; on failure the same worker gets the output and retries
  (`auto_fix_rounds`, default 2), then the job **escalates** to the next brain in the same worktree.
- Quota (rest until the reset in the error), missing sign-in, permission blocks and crashes also move down the chain.
- `ratings.jsonl` records per model and difficulty: passed (minus a little per automatic round), escalated, failed,
  and the director's accepted (merge) / rework (followup) / rejected (discard). Score = Laplace-smoothed success
  rate. Routing skips a brain with >= 4 events and score < 0.35 at that difficulty, except 15% of the time
  (exploration). `delegate.py ratings` shows the table.
- codex and grok are implemented but **disabled** (`brains.json` `disabled`). The claude CLI is never automatic.

## Sign-in and accounts

opencode needs none: a chain entry without a model runs its free built-in models (`opencode models`, the
`opencode/*` ones), the best `opencode_free_top` (3) by `opencode_prefer`, each with its own quota rest, so a
limit on one moves to the next. A worker that turns out signed out gets its own sign-in window opened on this
machine (at most once per `login_prompt_sec`, 15 min); when nothing else in the chain can work, the job waits up to
`login_wait_sec` (15 min) for that sign-in and then carries on (`phase: waiting_login`). agy runs with `SSH_CONNECTION=local`, so its login is a token file under its
home rather than the Windows credential store: sign-in is a link to click, and extra agy accounts run side by side
like the other CLIs' (`brains.json` `"agy_file_auth": false` turns this off; then `delegate.py agy-switch` swaps
the single login). The token file is plain, readable by this Windows user's processes. claude / codex / grok support
extra accounts through per-account config folders. Guide: `plugins/delegate/skills/orchestrate/references/auth-and-fallback.md`.

## Worker permissions (safety)

Defaults are each CLI's restricted headless mode; the plugin never widens them:

| CLI | Default command | Shell? |
|---|---|---|
| opencode | `run --standalone`, plus `OPENCODE_CONFIG_CONTENT` denying `external_directory` and `question` | per config; outside the worktree: denied |
| agy | `agy-real --sandbox --mode accept-edits -p`, in a worker home whose settings deny `command(*)` | no: denied, and it carries on (edits; the job's `check` runs the tests) |
| claude | `--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep -p` | no |
| codex | `exec -s workspace-write` | yes, sandboxed to the worktree |
| grok | `--permission-mode acceptEdits -p` | no |

Why the denies: in headless mode a tool that would need to *ask* is auto-rejected and the whole run ends with
nothing done (agy: `blocked`; opencode: exit 1). An explicit **deny** instead hands the model an error and it
carries on; verified for both. agy reads permissions only from `$HOME/.gemini/antigravity-cli/settings.json`, so its
workers run in `~/.claude-delegate/accounts/agy/default/` (the operator's settings + `agy_permissions`, seeded with
the operator's login token; the operator's own agy settings are never touched). A deny beats any allow there, so
`agy_permissions` is all-or-nothing per rule. `"agy_worker_home": false` in `brains.json` goes back to the
operator's home. A worker that shows no progress (logs, worktree, agy's own log) for `stall_sec` (4 min) is stopped
and the next brain takes over.

YOLO (opt-in): `"agy_yolo": true` runs agy workers with `--dangerously-skip-permissions`, so they run tests and fix
what fails themselves. Verified: deny rules still hold in that mode (`agy_yolo_deny`: rm/del/Remove-Item, git
push/reset/clean/checkout, curl/wget, ssh, installs …), but `--sandbox` does not keep writes inside the worktree
and an interpreter (`python -c …`) can do whatever a denied command could. Use it only where that is acceptable.
Other overrides go in `~/.claude-delegate/providers.json`:

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
