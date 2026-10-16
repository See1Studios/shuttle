# Sign-in, quota fallback, and agy account swaps

## Brains and chains

A **brain** is `cli:model@account`. Every job runs down a **chain** of brains: a difficulty tier
(`brains.json` `tiers`), or the chain led by a named `cli`. The runner moves to the next brain when the current one

| stop | detected by | what happens |
|---|---|---|
| quota / rate limit / outage | "429", "usage limit", "resets in 2h5m", 5xx … | its quota group rests until the reset (15 min if none given) |
| not signed in | "not logged in", "401", "OAuth … expired" … | its sign-in window opens on this machine; rests until signed in (6 h max); when the whole chain is out, the job waits for the sign-in (`phase: waiting_login`, up to `login_wait_sec`) and carries on |
| blocked by its permission mode | "headless mode cannot prompt … auto-denied" (exit 0, nothing done) | rests 6 h; rare now: workers get explicit denies, which they work around |
| stuck | no change in its logs, worktree or agy's own log for `stall_sec` (4 min) | stopped; its quota group rests 15 min; next brain takes over |
| failed (exit ≠ 0) | anything else | next brain takes over (no rest) |
| check still failing after its automatic rounds | the job's `check` | **escalates** to the next brain (rated `escalated`) |

The next brain continues in the same worktree and is told what the previous one left. Results show
`fell_back_from` and `escalated`.

Quota groups: one per account, except agy, whose limits are per model family **and per Google account**:
`agy@<email>#gemini` and `agy@<email>#claude-gpt`. Swapping agy's account therefore frees its quota at once, and
the old account's rests are remembered for when it comes back.

## Workers on this host

| CLI | Sign-in | Notes |
|---|---|---|
| opencode | none needed | without a model: its best free built-in models (`opencode_prefer`, top `opencode_free_top`), one quota rest each; always `--standalone` so it works in the worktree |
| agy | Google account, token file per account home | workers run in `accounts/agy/default` (seeded with the operator's token, settings deny shell commands); extra accounts with `shuttle_login("agy", name)` |
| claude | `claude auth login` | spends Claude quota; never automatic |
| codex, grok | — | disabled (no subscription); implemented, enable in `brains.json` |

## Checking sign-in

1. `shuttle_auth()` — every enabled CLI and account with `logged_in`; each one missing gets its sign-in window
   opened on this machine (`login_window` in the row; claude only when named). Tell the operator to finish it there.
   You never type or see credentials. `shuttle_dashboard()` gives them a page with the same state and a login
   button per CLI.
2. Jobs do this by themselves: a worker found signed out opens its sign-in window, the next brain takes over, and
   when none is left the job waits for the sign-in and carries on.
3. Extra accounts (more quota) for claude / codex / grok / opencode: `shuttle_login(cli, "<name>")` creates
   `~/.shuttle/accounts/<cli>/<name>/` and points the CLI there (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
   `GROK_HOME`, `XDG_DATA_HOME`). The account joins that CLI's chains. `env.json` there may hold an API key; it is
   the operator's secret — never read it out.

## agy accounts (file auth)

Every agy run (worker, auth probe, sign-in window) gets `SSH_CONNECTION=local`. agy then takes its ssh path and
keeps the login in a token file under its home (`~/.gemini/antigravity-cli/`) instead of the Windows credential
store's single `gemini:antigravity` entry. So:

- A sign-in is a link to click, with no pasted-code deadline. By hand on the host (cmd, two lines):
  `set SSH_CONNECTION=local` then `agy`. A web-SSH session reads the same file and does not ask again.
- `shuttle_login("agy", "<name>")` makes `~/.shuttle/accounts/agy/<name>/`, points agy's `HOME` /
  `USERPROFILE` there and opens agy's sign-in for it. Accounts run side by side and join agy's chains.
- Trade-off: the token is a plain file, readable by anything running as this Windows user, not an encrypted
  credential-store entry. Never read it out. `"agy_file_auth": false` in `brains.json` restores the credential
  store (one account; swap below).
- After first turning file auth on, the default account is signed out until it is signed in once this way.

## Swapping agy's Google account in place (legacy; one-home setups)

With `agy_file_auth` off, agy keeps a single login in the Windows credential store, so accounts cannot run side
by side; they are swapped through agy's own OAuth sign-in:

1. Ask the operator: the current agy account gets signed out.
2. `shuttle_agy_switch(confirm_logout=true)` drives agy in a pseudo console: `/logout`, Google OAuth, and returns
   the sign-in `url` (plus `email_before`).
3. Give the operator the link. They open it, sign in with the other Google account, and paste back the response
   code the page shows.
4. `shuttle_agy_code(code)` types it into agy and returns the signed-in `email`. The code is used once and not
   stored. Then `shuttle_chain("normal")` shows agy available again.

Without an agent: `python shuttle.py agy-switch` (prints the link, prompts for the code). Needs `pip install pywinpty`.

## Configuration (operator-owned, optional): `~/.shuttle/brains.json`

```json
{
  "tiers": {
    "easy":   ["opencode", "agy:gemini-3.8-flash-medium", "agy:gemini-3.8-flash-high"],
    "normal": ["opencode", "agy:gemini-3.8-flash-high", "agy:claude-sonnet-5-5-high"],
    "hard":   ["agy:claude-sonnet-5-5-high", "agy:gemini-3.8-flash-high"]
  },
  "disabled": ["codex", "grok"],
  "ratings": { "min_events": 4, "min_score": 0.35, "explore": 0.15 },
  "auth_rest_sec": 21600,
  "login_prompt_sec": 900,
  "login_wait_sec": 900,
  "opencode_prefer": ["deepseek", "glm", "nemotron-3-ultra", "longcat", "mimo", "big-pickle"],
  "opencode_free_top": 3,
  "exclude": ["/.shuttle/", "graft/.cache/", "graft/.graph/"],
  "commit_rounds": false
}
```

`~/.shuttle/providers.json` changes a CLI's command line (e.g. permissions) or adds a custom CLI; see the
README. Worker permissions are widened only there, by the operator.
