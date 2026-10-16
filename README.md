<p align="center">
  <img src="assets/logo.svg" width="120" alt="Shuttle">
</p>

<h1 align="center">Shuttle</h1>

<p align="center">
  <em>Your agent writes the brief. Cheaper workers write the code.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/github/stars/See1Studios/shuttle?style=flat-square&color=111111&label=stars" alt="Stars">
  <img src="https://img.shields.io/badge/version-0.3.0-111111?style=flat-square" alt="Version 0.3.0">
  <img src="https://img.shields.io/badge/works%20with-Claude%20%7C%20Codex%20%7C%20Antigravity%20%7C%20Grok%20%7C%20OpenCode-111111?style=flat-square" alt="Works with 5 agents">
  <img src="https://img.shields.io/badge/python-3.10%2B-111111?style=flat-square" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT license">
</p>

<p align="center">
  <strong>Free models first &middot; Isolated git worktrees &middot; Auto check &amp; escalate &middot; Tiered review &middot; One hooked commit</strong><br>
  <sub>Turns Claude Code, OpenAI Codex, Antigravity (<code>agy</code> &amp; IDE), Grok Build, and OpenCode into a token-miserly director that farms out implementation work to headless worker CLIs, runs acceptance checks automatically, learns which model passes what, and reviews only the final diff.</sub>
</p>

---

Your frontier agent session is expensive. Burning its context window on boilerplate edits, test-fix loops, and multi-file mechanical refactors drains your quota before the hard problems even start.

**Shuttle** flips the workflow: your main agent becomes the **director**. It spends tokens only where judgment matters—writing a crisp brief with an executable acceptance check, and reviewing the finished result. Everything in between runs headless in an isolated git worktree on cheaper or free worker CLIs.

## Before / after

Without Shuttle, your primary session reads 20 files, edits 6, runs pytest three times, hits a rate limit halfway through, and leaves your working tree half-modified.

With Shuttle:

```text
/shuttle normal add exponential backoff to the HTTP client
```

```text
Director: brief + check ──> runner ──> opencode (free) ──check fails──> agy Gemini ──> agy Claude group
                                         │  quota / signed out / blocked / stalled -> next brain, same worktree
            verdict, diffstat, report <──┘  ratings.jsonl learns who passes what -> routing skips proven-weak brains
Director: tiered review (machine -> shape -> worker 2nd opinion -> targeted read) -> merge (one hooked commit) | rework | discard
```

Your working tree stays untouched until you approve the merge.

## How it works

When you shuttle a task, the runner walks a live ladder of worker brains without waking the director:

```text
1. Pick the chain by difficulty (easy / normal / hard) and live model listings
2. Skip brains that are resting (quota / auth) or proven weak at this difficulty (ratings.jsonl)
3. Spawn the worker headless in an isolated git worktree (.shuttle/task.md)
4. Run the acceptance check after the worker exits
5. Check fails? Give the same worker the failure output to auto-fix (up to auto_fix_rounds = 2)
6. Still failing, stalled, blocked, or out of quota? Escalate to the next brain in the same worktree
7. Hand the director a lean verdict (check status, diffstat, worker report) — never the raw logs
```

### Difficulty routing & escalation

| Difficulty | Default chain (built from live model lists) |
|---|---|
| `easy` | `opencode` free &times;3 &rarr; `agy` Gemini Flash (low &rarr; medium &rarr; high) |
| `normal` *(default)* | `opencode` free &times;3 &rarr; `agy` Gemini Flash high &rarr; Claude Sonnet &rarr; Claude Opus &rarr; Gemini Argon (medium) |
| `hard` | `agy` Gemini Argon &rarr; Claude Opus &rarr; Claude Sonnet (high) &rarr; Gemini Flash high |

- **Live model discovery**: `agy` rungs come from `agy models` (cached for 6 h, so newly released models are picked up with zero config), ranked smartest-first by `agy_rank` (`argon > opus > sonnet > flash`, newest version of each), with effort scaled by difficulty (`easy` &rarr; low, `normal` &rarr; medium, `hard` &rarr; high). Quota pools are tracked separately (`argon`, `gemini`, `claude-gpt`).
- **Zero-login free tier first**: `opencode` free models come from `opencode models` (`opencode/*`), ordered by `opencode_prefer` (`space-bunny` first), each with its own independent quota rest.
- **Self-learning ratings (`ratings.jsonl`)**: Every job records per-model &times; difficulty outcomes (`passed`, `escalated`, `failed`) and director verdicts (`accepted` on merge, `rework` on followup, `rejected` on discard). Routing skips any brain with &ge; 4 events and a Laplace-smoothed score &lt; 0.35 at that difficulty, while keeping a 15% exploration chance.
- **Strict worker isolation**: Before spawning a worker, Shuttle strips host-agent session variables (`CLAUDE*`, `CODEX_*`, `ANTIGRAVITY_*`, `GROK_*`, `OPENCODE_*`) and sets `SHUTTLE_WORKER=1`. When `mcp_server.py` sees `SHUTTLE_WORKER=1`, it registers **zero tools**, preventing recursive sub-delegation loops.

## Install

**Requirements**: Python 3.10+, `pip install mcp pywinpty` (`mcp` 1.x or 2.x; `pywinpty` is only used for the interactive `agy` account swap on Windows), `git`, and at least one worker CLI (`opencode`, `agy`, `claude`, `codex`, or `grok`) on `PATH`.

### All installed agents (one command)

From a checkout of this repository, install or update `shuttle` across every coding agent found on your machine:

```bash
python scripts/shuttle.py install
```

Or target a single host:

```bash
python scripts/shuttle.py install claude    # or: codex | agy | grok | opencode
```

---

### Claude Code

```text
/plugin marketplace add See1Studios/shuttle
```
```text
/plugin install shuttle@shuttle
```
*(Send as two separate prompts in Claude Code CLI or the Desktop app's Code tab, or run `claude plugin marketplace add See1Studios/shuttle && claude plugin install shuttle@shuttle` from your shell.)*

Registers the `shuttle` MCP server and the `/shuttle`, `/shuttles`, `/shuttle-auth`, and `orchestrate` skills.

### Codex

```bash
codex plugin marketplace add See1Studios/shuttle
codex plugin add shuttle@shuttle
```

Covers both the Codex CLI and the Codex Desktop app (restart the app after installing so it picks up the `shuttle` MCP server and skills). In Codex, invoke skills with `$` or `@` (e.g. `$shuttle`, `$shuttles`, `$shuttle-auth`) or ask in natural language.

### Antigravity (`agy` CLI & IDE)

```bash
agy plugin install https://github.com/See1Studios/shuttle --force
```

*(If `agy` on your PATH is a wrapper, use `agy-real plugin install https://github.com/See1Studios/shuttle --force`, or pass `.` from a local clone.)*

Installs the 4 skills and registers the `shuttle` MCP server via [`mcp_config.json`](mcp_config.json). When you open a local checkout in Antigravity, [`.agents/plugins.json`](.agents/plugins.json) and [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json) also enable workspace auto-discovery.

### Grok Build

```bash
grok plugin install See1Studios/shuttle --trust
```

Enable the plugin in `~/.grok/config.toml` (`python scripts/shuttle.py install grok` does both automatically):

```toml
[plugins]
enabled = ["shuttle"]
```

Start a new session (or reload plugins in `/plugins`). Skills are available as `/shuttle`, `/shuttles`, and `/shuttle-auth`, backed by the `shuttle` MCP server.

### OpenCode

Run the installer to register the MCP server in `~/.config/opencode/opencode.json` and copy the skills into `~/.config/opencode/skills/`:

```bash
python scripts/shuttle.py install opencode
```

Or add the MCP server manually to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "servers": {
      "shuttle": {
        "type": "local",
        "command": ["python", "/path/to/shuttle/scripts/mcp_server.py"]
      }
    }
  }
}
```

### Uninstall

| Host | Command |
|------|---------|
| Claude Code | `claude plugin uninstall shuttle@shuttle && claude plugin marketplace remove shuttle` |
| Codex | `codex plugin remove shuttle@shuttle && codex plugin marketplace remove shuttle` |
| Antigravity | `agy-real plugin uninstall shuttle` |
| Grok Build | `grok plugin remove shuttle` (and remove `"shuttle"` from `~/.grok/config.toml`) |
| OpenCode | Remove `"shuttle"` from `~/.config/opencode/opencode.json` and delete `~/.config/opencode/skills/{orchestrate,shuttle,shuttles,shuttle-auth}` |

Runtime state lives in `~/.shuttle/` (or `$SHUTTLE_HOME`). Delete that directory if you also want to remove job history, worktrees, ratings, and isolated worker account tokens.

## Commands & Tools

### Slash commands / skills

| Command / Skill | What it does |
|-----------------|--------------|
| `/shuttle [easy\|normal\|hard\|cli] <task>` | Write a brief + acceptance check, shuttle the task to an isolated worktree, run tiered review, and ask before landing. |
| `/shuttles [id] [review\|merge\|discard\|followup ...]` | List active/recent shuttle jobs, or inspect, review, rework, merge, or discard a job. |
| `/shuttle-auth [cli]` | Check sign-in status across worker CLIs, open sign-in windows where missing, or swap `agy`'s Google account. |
| `orchestrate` | Always-on director procedure: triggers automatically when you say *"shuttle this"*, *"have opencode/agy do it"*, *"farm this out"*, or ask for parallel/competing workers. |

### MCP tools (`shuttle` server) & CLI

Every operation is available both as an MCP tool for the director agent and as a standalone CLI subcommand via `python scripts/shuttle.py <cmd>`:

| MCP Tool | CLI Subcommand | Purpose |
|----------|----------------|---------|
| `shuttle_start` | `start` | Start a background worktree job with a brief, difficulty, and `--check` command |
| `shuttle_compete` | `compete` | Fan out the same brief to multiple brains in parallel worktrees to compare approaches |
| `shuttle_wait` | `wait` | Block until one or all running jobs finish (including auto-fix & escalation rounds) |
| `shuttle_result` | `result` | Read the lean verdict: check status, diffstat, worker report, and optional targeted diff |
| `shuttle_check` | `check` | Re-run a job's acceptance check (or run an ad-hoc command) inside its worktree |
| `shuttle_followup` | `followup` | Send rework instructions to the same worktree (records a `rework` rating) |
| `shuttle_review` | `review` | Ask a separate read-only worker brain for a second-opinion code review on the diff |
| `shuttle_merge` | `merge` | Land the approved worktree onto your branch (`squash` default, `ff`, or `merge`) |
| `shuttle_discard` | `discard` | Delete the worktree and branch, and record a `rejected` rating |
| `shuttle_cancel` | `cancel` | Stop a running worker process |
| `shuttle_list` / `shuttle_status` | `list` / `status` | List jobs or inspect a single job's live status and timings |
| `shuttle_chain` / `shuttle_ratings` / `shuttle_limits` | `chain` / `ratings` / `limits` | Inspect difficulty chains, model scores, and resting quota groups |
| `shuttle_auth` / `shuttle_login` | `auth` / `login` | Check worker sign-ins or open a worker CLI's login window |
| `shuttle_agy_switch` / `shuttle_agy_code` | `agy-switch` / `agy-code` | Swap `agy`'s Google account via OAuth link + pasted code |
| `shuttle_dashboard` | `dashboard` | Open the local web dashboard at `http://127.0.0.1:8765/` |

### Local management dashboard

Starting a job automatically brings up the local dashboard at `http://127.0.0.1:8765/` (disable with `"dashboard_on_start": false` in `brains.json` or `SHUTTLE_NO_DASHBOARD=1`). You can also launch it anytime:

```bash
python scripts/shuttle.py dashboard
```

Shows live sign-in status with one-click login buttons, resting quota groups (with a clear button), `opencode` free models, difficulty chains, model ratings, and running/recent jobs.

## Sign-in and accounts

- **`opencode` needs no login**: Chain entries without a specific model automatically expand into the top `opencode_free_top` (3) free built-in models from `opencode models`.
- **Automatic sign-in prompt & wait**: If a worker turns out to be signed out, Shuttle opens its login console window on your machine (throttled to once per `login_prompt_sec`, 15 min). If every brain in the chain is signed out or resting, the job enters `phase: waiting_login` for up to `login_wait_sec` (15 min) and resumes automatically as soon as you finish signing in.
- **Multi-account `agy` via file store**: Shuttle runs `agy` with `SSH_CONNECTION=local` so credentials live in a per-account token file under `~/.shuttle/accounts/agy/<account>/` instead of fighting over a single OS credential-store slot. Extra accounts run side by side without logging each other out. Set `"agy_file_auth": false` in `~/.shuttle/brains.json` to use the OS credential store instead (`shuttle.py agy-switch` then swaps the single login).
- **Per-account homes for other CLIs**: `claude`, `codex`, and `grok` also support named accounts via isolated config directories under `~/.shuttle/accounts/`. Full details: [`skills/orchestrate/references/auth-and-fallback.md`](skills/orchestrate/references/auth-and-fallback.md).

## Worker permissions (safety)

Workers run headless with restricted permissions by default—Shuttle never widens them unless you opt in:

| Worker CLI | Default headless command | Shell access? |
|---|---|---|
| `opencode` | `run --standalone`, plus `OPENCODE_CONFIG_CONTENT` denying `external_directory` and `question` | Inside worktree only; external reads/writes denied |
| `agy` | `agy-real --sandbox --mode accept-edits -p` in a worker home whose `settings.json` denies `command(*)` | No (`command(*)` denied; runner executes `--check`) |
| `claude` | `--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep -p` | No |
| `codex` | `exec -s workspace-write` | Yes, sandboxed to the worktree |
| `grok` | `--permission-mode acceptEdits -p` | No |

**Why explicit deny rules matter**: In headless mode, any tool call that would *prompt for confirmation* is auto-rejected by the CLI and aborts the entire run (`agy`: `blocked`; `opencode`: exit 1). An explicit **deny** rule instead returns a permission error to the model so it continues editing files and lets the runner's `check` command run the test suite.

- **Stall watchdog**: A worker that produces no log output and no worktree changes for `stall_sec` (4 min) is terminated as `stalled`, and the next brain in the chain takes over.
- **Outside-worktree guard**: Before and after each worker run, Shuttle compares `git status` in the main repository and surfaces any unexpected change outside the worktree in the job's `warnings`.
- **Opt-in YOLO mode**: Setting `"agy_yolo": true` in `~/.shuttle/brains.json` runs `agy` workers with `--dangerously-skip-permissions` while keeping `agy_yolo_deny` rules (`rm`, `del`, `Remove-Item`, `git push/reset/clean/checkout`, `curl`, `wget`, `ssh`, package installs). Custom CLI commands can also be configured in `~/.shuttle/providers.json`.

## Landing and git hooks

- **Hooks run once, at landing**: Worker rounds stage changes (`git add -A`) inside the worktree without committing by default (`"commit_rounds": false`), so slow pre-commit hooks do not fire on every retry round.
- **Clean single commit on merge**: `merge` (`squash`, default) applies the final worktree diff to your checked-out branch and creates a single commit—running your repository's commit hooks once. If a commit hook refuses, the error is surfaced in `commit_error` while keeping your changes staged.
- **Safety checks**: Landing refuses if your main working tree has uncommitted tracked modifications, and workers never push.

## Repository layout

| Path | Role |
|---|---|
| [`scripts/shuttle.py`](scripts/shuttle.py) | Worktree runner, background job lifecycle, check/auto-fix/escalate loop, merge/discard, multi-agent installer |
| [`scripts/brains.py`](scripts/brains.py) | Difficulty ladders, live model discovery, quota-group rests, sign-in probes, multi-account homes |
| [`scripts/ratings.py`](scripts/ratings.py) | Per-model &times; difficulty Laplace-smoothed score tracker and weak-brain filter |
| [`scripts/dashboard.py`](scripts/dashboard.py) | Local HTTP management dashboard (`127.0.0.1:8765`, CSRF-guarded via `X-Shuttle: 1`) |
| [`scripts/mcp_server.py`](scripts/mcp_server.py) | FastMCP server exposing `shuttle_*` tools to the director (hidden when `SHUTTLE_WORKER=1`) |
| [`scripts/signin.py`](scripts/signin.py) / [`scripts/agy_login.py`](scripts/agy_login.py) | Interactive login window launcher and pseudo-console `agy` OAuth account switcher |
| [`skills/`](skills/) | Unified skills (`orchestrate`, `shuttle`, `shuttles`, `shuttle-auth`) and review/auth references |
| [`plugin.json`](plugin.json), [`.claude-plugin/`](.claude-plugin/), [`.codex-plugin/`](.codex-plugin/), [`.grok-plugin/`](.grok-plugin/), [`.agents/`](.agents/) | Native plugin manifests, marketplace definitions, and MCP configs across all 5 host agents |
| [`tests/`](tests/) | Hermetic unit test suite (`unittest`, zero live worker invocations required) |

## Development

Run the full hermetic test suite (39 tests covering `agy` file auth, commit-hook visibility, free-model ladders, login windows, worker guards, director env isolation, manifests, and host installers):

```bash
python -m unittest discover -s tests -v
```

Validate plugin manifests across host CLIs:

```bash
claude plugin validate .
agy-real plugin validate . --strict --json
grok plugin validate .
```

## License

MIT.
