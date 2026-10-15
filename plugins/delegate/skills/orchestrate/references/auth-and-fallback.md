# Sign-in and quota fallback

## Brains and chains

A **brain** is `cli:model@account`. Every job runs down a **chain** of brains. When the running brain stops
because of a quota / rate limit / outage, or because it is not signed in, the runner:

1. rests that brain's **quota group** until its reset (parsed from "resets in 2h5m"; 15 min when no time is given;
   6 h for a missing sign-in),
2. starts the next brain that is not resting **in the same worktree**, telling it that partial changes may be there.

The result shows `fell_back_from` (which brains were skipped and why). A job fails with `needs_login` when every
brain was out; give those login commands to the operator.

Quota groups:

| CLI | Quota group | Meaning |
|---|---|---|
| agy | `agy@<account>#gemini`, `agy@<account>#claude-gpt` | agy's limits are per model family: when Gemini models are spent, Claude/GPT models still work, and vice versa |
| others | `<cli>@<account>#*` | one pool per account |

Default chain for `delegate_start(cli="agy")`, with one account:

```
agy:gemini-3.8-flash-high@default   -> agy@default#gemini
agy:claude-sonnet-5-5-high@default  -> agy@default#claude-gpt
codex@default -> grok@default -> claude@default      (cross-CLI fallback)
```

With extra accounts the order is model-first: each model on every account before the next model. Check before a
big fan-out with `delegate_chain(cli)`; see current rests with `delegate_limits()`.

Override per job with `chain=["agy:gemini-3.8-flash-high", "codex@work", "grok"]`, or turn cross-CLI fallback off
with `fallback=false` (compete mode never crosses CLIs, so candidates stay distinct).

## Accounts

`default` is each CLI's normal login. Extra accounts live in `~/.claude-delegate/accounts/<cli>/<name>/`; the
runner points the CLI there through its config-home variable, so they never disturb the operator's own login:

| CLI | Variable | Login command (run by `delegate_login`) |
|---|---|---|
| codex | `CODEX_HOME` | `codex login` |
| claude | `CLAUDE_CONFIG_DIR` | `claude auth login`, or `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` in `env.json` |
| grok | `GROK_HOME` | `grok login` (`--device-auth` without a browser), or `XAI_API_KEY` in `env.json` |
| opencode | `XDG_DATA_HOME` | `opencode auth login` |
| agy | — | Windows keeps agy's login in the credential store under one fixed name, so only the default account works; agy falls back across its two model groups instead |

`env.json` in an account folder adds environment variables for that account only (API keys, tokens). It holds
secrets: it is the operator's file. Never read its values out loud or copy them anywhere.

## The sign-in procedure (what you do)

1. `delegate_auth()` — every installed CLI and account with `logged_in`.
2. For each `logged_in: false` the operator wants to use: tell them the `login` command from the result, and offer
   to open it: `delegate_login(cli, account)` opens a new console window running the CLI's own sign-in. **Ask
   first**; the operator completes the browser / device-code step. You never type or see credentials.
3. To add a second account of a CLI (more quota): `delegate_login(cli, "<name>")` creates the account folder and
   opens its sign-in. It joins every default chain of that CLI automatically.
4. After the operator says it is done, run `delegate_auth(cli)` again to confirm, and `delegate_limits(clear="<cli>@<account>")`
   if an old rest for that account is still listed.

The Claude desktop app's own session is not the `claude` CLI's login: a worker `claude` needs its own sign-in.

## Configuration (operator-owned, optional)

`~/.claude-delegate/brains.json`:

```json
{
  "models":   { "agy": ["gemini-3.8-flash-high", "claude-sonnet-5-5-high"], "codex": [""] },
  "fallback": { "codex": ["agy", "grok"], "agy": ["codex", "grok", "claude"] },
  "order": "model-first",
  "auth_rest_sec": 21600,
  "exclude": ["/.delegate/", "graft/.cache/", "graft/.graph/"]
}
```

`~/.claude-delegate/providers.json` changes a CLI's command line or adds a custom one (see the README). Worker
permissions are widened only there, by the operator.
