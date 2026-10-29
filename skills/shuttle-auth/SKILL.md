---
name: shuttle-auth
description: Check which worker CLIs and accounts are signed in, guide sign-in, submit Google OAuth codes, or add accounts for more quota
argument-hint: "[cli] [account]"
---

Use the `orchestrate` skill (read `references/auth-and-fallback.md` and `references/dashboard-guide.md`).

Arguments: $ARGUMENTS

### 1. Check State & Offer Dashboard
- Call `shuttle_auth(open_login=...)` to inspect current sign-ins across worker CLIs.
- Provide the operator with the local dashboard link: **`http://127.0.0.1:8790/`**.
  The dashboard shows all sign-ins, resting quota groups, and allows one-click logins and direct code submission.

### 2. Login Procedure per Worker CLI

- **opencode**:
  - **Zero sign-in required!** opencode uses its free built-in models (`opencode/*`) out of the box.
  - Never prompt the operator to sign into opencode.

- **Antigravity (`agy`)**:
  - `agy` uses Google OAuth. When `shuttle_login("agy", account)` runs:
    1. A browser window opens with the Google OAuth consent page.
    2. The operator selects their Google account and authorizes access.
    3. Google displays a response code ("authorization code") on the screen.
    4. **Submitting the code**:
       - **Option A (Dashboard, recommended)**: The operator pastes the code into the "응답 코드 입력" box in the agy row at `http://127.0.0.1:8790/` and clicks "제출".
       - **Option B (Chat)**: If the operator pastes the code here in chat, immediately submit it using `shuttle_agy_code(code)`.
    5. Authentication completes instantly in the background and the token is saved.
  - Extra agy accounts for more quota: `shuttle_login("agy", "<name>")` creates an isolated file-auth home for that account.

- **Claude Code (`claude`)**:
  - Spends Claude quota; never automatic.
  - `shuttle_login("claude", account)` opens an interactive terminal window to run `claude auth login`. The operator completes browser auth in that window.

- **Codex & Grok Build**:
  - Disabled by default (in `~/.shuttle/brains.json`).
  - When enabled, `shuttle_login("codex")` or `shuttle_login("grok")` opens a console window for authentication.

### 3. Confirm & Lift Cooldowns
- When the operator completes sign-in, run `shuttle_auth()` to verify.
- Clear any resting limits for that worker with `shuttle_limits(clear="<cli>@<account>")` so the worker is immediately usable in chains.
- Conclude with `shuttle_limits()` to show remaining active quota pools.
