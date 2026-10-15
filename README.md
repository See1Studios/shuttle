# claude-delegate

A Claude Code plugin that turns Claude into a **director**: it writes a brief, hands the work to another agent CLI
(codex, claude, grok, agy/Gemini, opencode) running headless in an isolated git worktree, waits, reviews the diff,
sends rework, and lands the result. Modelled on PrivateEngine's worktree delegation
(`engine/dev/worktree_runner.py`, `engine/tools/brain_limits.py`), without its ticket governance.

```
you ──> Claude (director) ──brief──> worker CLI in ~/.claude-delegate/worktrees/<id>  (branch delegate/<id>)
                 ^                         │  quota / not signed in? -> next brain in the chain, same worktree
                 └──── report + diff ──────┘
        review (+ optional 2nd-opinion review by another model) -> rework round | squash-merge | discard
```

## What is in it

| Part | Path | What it does |
|---|---|---|
| Runner + CLI | `plugins/delegate/scripts/delegate.py` | worktrees, background jobs, briefs, commits, diff/result, rework rounds, second-opinion reviews, merge/discard |
| Brains | `plugins/delegate/scripts/brains.py` | model/account/CLI fallback chains, quota-group rests, sign-in checks and login launcher |
| MCP server | `plugins/delegate/scripts/mcp_server.py` | the same operations as `delegate_*` tools for Claude |
| Skill | `plugins/delegate/skills/orchestrate/` | the director's procedure, review checklist, sign-in & fallback guide |
| Commands | `plugins/delegate/commands/` | `/delegate`, `/delegations`, `/delegate-auth` |

## Install

Requirements: Python 3.10+, `pip install mcp` (1.x or 2.x), git, and the worker CLIs you want on `PATH`.

```bash
claude plugin marketplace add C:/Users/user/Documents/GitHub/claude-delegate
```

```bash
claude plugin install delegate@claude-delegate
```

Restart the Claude session; the `delegate_*` tools and `/delegate` appear.

## Use

- `/delegate-auth` — which CLIs/accounts are signed in; opens a sign-in window where one is missing.
- `/delegate codex add retry with backoff to the HTTP client, with tests` — brief, run, review, land.
- `/delegate codex,grok <task>` — compete mode: both do it, Claude picks the better diff.
- `/delegations` — list jobs; `/delegations <id> review|merge|discard|followup <feedback>`.
- Or just ask: "이 작업은 codex한테 시키고 결과 리뷰해줘".

Without Claude: `python plugins/delegate/scripts/delegate.py --help`
(`clis`, `auth`, `login`, `chain`, `limits`, `start`, `list`, `status`, `wait`, `result`, `followup`, `review`,
`merge`, `cancel`, `discard`).

## Quota fallback and accounts

Each job runs down a chain of brains (`cli:model@account`). When a brain hits a quota / rate limit / outage or is
not signed in, its quota group is rested until the reset time in the error, and the next brain continues in the
same worktree. agy has two quota groups (Gemini models; Claude and GPT models), so the default agy chain is
Gemini first, then Claude, then the other CLIs. Inspect with `delegate.py chain agy`; current rests with
`delegate.py limits`.

More accounts = more quota: `delegate.py login codex work` creates `~/.claude-delegate/accounts/codex/work/`,
points `CODEX_HOME` at it and opens `codex login` in a new window. The account joins codex's chain. Same for
claude (`CLAUDE_CONFIG_DIR`), grok (`GROK_HOME`) and opencode (`XDG_DATA_HOME`). An account's `env.json` can hold an
API key or token instead. agy on Windows keeps one login in the credential store, so it has only its default
account. Full guide: `plugins/delegate/skills/orchestrate/references/auth-and-fallback.md`.

## Worker permissions (safety)

Workers run with each CLI's restricted headless mode by default:

| CLI | Default | Can run tests? |
|---|---|---|
| codex | `exec -s workspace-write` (OS sandbox, writes only in the worktree) | yes |
| claude | `--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep` | no |
| grok | `--permission-mode acceptEdits` | no |
| agy | `--sandbox --mode accept-edits` | no (shell auto-denied headless) |
| opencode | `run` with opencode's own permission config | per config |

Reviewers run read-only in an empty folder holding only the review brief. The director (Claude) runs the tests
itself in the worktree during review. To give a worker more (e.g. shell for claude), the operator edits
`~/.claude-delegate/providers.json`; the plugin never widens permissions itself. Example:

```json
{
  "claude": { "work": ["claude", "--permission-mode", "acceptEdits",
                        "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(pytest:*),Bash(python -m pytest:*)", "-p"] },
  "mycli":  { "work": ["mycli", "run"], "review": ["mycli", "run", "--read-only"], "model_flag": "--model",
              "author": ["MyCLI", "mycli@localhost"] }
}
```

## State

`~/.claude-delegate/` (or `$DELEGATE_HOME`): `jobs/<id>/` (job.json, task.md, per-attempt stdout/stderr logs),
`worktrees/<id>/`, `accounts/`, `limits.json`, optional `brains.json` and `providers.json`. Discard or merge jobs
to remove their worktrees.

## Notes

- Landing (`merge`) refuses while the main working tree has uncommitted tracked changes, and runs the repo's
  commit hooks. Workers never push.
- A repo with its own delegation rules (e.g. PrivateEngine's tickets and `worktree_runner`) keeps them; the skill
  tells Claude to follow the repo's AGENTS.md first.
- `/.delegate/` and tool caches (`graft/.cache/`, `graft/.graph/`) are added to the repo's `.git/info/exclude` so
  they never enter a commit.
