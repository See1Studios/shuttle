# Shuttle Management Dashboard Guide (Mission Control)

The Shuttle local dashboard runs at **`http://127.0.0.1:8790/`** (lightweight, zero-dependency, stdlib Python HTTP server).
It is automatically started when a shuttle job begins (unless disabled), or on demand via `shuttle_dashboard()` / `python scripts/shuttle.py dashboard`.

Security note: The dashboard is strictly bound to `127.0.0.1` and CSRF-protected via mandatory `X-Shuttle: 1` headers on all state-changing API calls.

---

## 1. Key Capabilities of the Dashboard

### 🚀 1) New Task Launcher (`+ 새 작업 시작`)
- Operators can launch shuttle tasks directly from the web interface without using terminal or chat prompts.
- **Fields**:
  - **Task Brief (필수)**: Detailed task instructions for the worker agent.
  - **Difficulty**: `easy` (free/low-cost models first), `normal` (default: opencode → agy Flash/Claude/Argon), `hard` (top-tier reasoning models).
  - **Specific CLI (선택)**: Force `opencode`, `agy`, `claude`, `codex`, or `grok`, or leave as auto-routing.
  - **Acceptance Check (--check, 선택)**: Test command executed after each round (e.g. `pytest tests/test_foo.py` or `npm test`). If the test fails, the worker auto-retries with the failure output.
- Submitting automatically starts the job in the background, updates the jobs table, and opens the job drawer.

### 📑 2) Interactive Slide-over Job Drawer
Clicking any job row or its **"상세"** button opens a full-height slide-over drawer with 4 dedicated tabs:
1. **워커 보고서 (Report)**: Renders `REPORT.md` written by the worker CLI explaining changes made, design decisions, and testing results.
2. **코드 변경 (Diff)**: Full unified diff with clean syntax highlighting (additions in green, deletions in red, hunks in purple, diff headers in bold). Displays diffstat at the top with a **"Diff 복사"** button.
3. **실행 로그 (Logs)**: Full stdout tail, stderr tail, and check command outputs with a one-click **"로그 복사"** button.
4. **상세 정보 (Meta)**: Status, phase, elapsed time, worker PID, branch, worktree directory, exit code, and error messages.

### ⚡ 3) Direct Actions from the Drawer
- **머지 (Merge to main)**: Available when status is `done`. Prompts for a commit message (or uses default), runs a clean squash merge into the target branch, executes repository commit hooks once, and cleans up the worktree.
- **추가 수정 (Followup / Rework)**: Prompts for feedback/instructions, passes them back to the worker in the existing worktree, and triggers another round (records a `rework` event in ratings).
- **체크 재실행 (Re-check)**: Runs the job's acceptance check command on demand inside the worktree and reports whether it passed or failed.
- **작업 취소 (Cancel)**: Terminate a currently running or starting worker process.
- **작업 및 워크트리 버리기 (Discard)**: Delete the worktree and branch, and record a `rejected` rating.

### 🔑 4) Worker Authentication, One-Click Auto Login & Multi-Account Management
- Shows real-time login state for all CLI workers and all registered accounts.
- **⚡ 원클릭 자동 로그인 (One-Click Auto Login)**:
  - Clicking **"⚡ 원클릭 로그인"** launches Microsoft Edge or Chrome with remote debugging (CDP) enabled.
  - The browser opens Google OAuth. Once the operator selects their Google account and authorizes, Shuttle automatically detects the response code from the browser tab URL or clipboard.
  - Shuttle immediately submits the code to agy and closes the OAuth callback tab automatically — zero copy-paste or terminal typing required!
- **➕ 다중 계정 등록 (Multi-Account Registration & Auto-Fallback)**:
  - Click **"+ 계정 추가"** in the Auth header to register additional accounts (e.g. `work`, `acc2`).
  - Shuttle creates an isolated configuration directory under `~/.shuttle/accounts/<cli>/<name>/` and initiates one-click login.
  - When running tasks, Shuttle spreads each model across all registered accounts. If `default` exhausts its quota (429), Shuttle seamlessly rolls over to `acc2`, then `acc3`!
- **🛡️ 고비용 모델 자동 제외 (Exclude Argon & Opus)**:
  - High-token models like Gemini Argon and Claude Opus are excluded from auto-routing and default ladders (`exclude_models: ["argon", "opus"]`), keeping token consumption strictly under control.
- **수동 입력 지원 (Manual Code Fallback)**:
  - If auto-detection is not used, operators can still click the authorization link and paste the code into the input box on the dashboard.

### 📊 5) Model Performance & Ratings (`ratings.jsonl`)
- Live leaderboard showing how each brain (`cli:model`) performs at each difficulty level.
- Visual score bars with Laplace-smoothed success rate (`0.00` ~ `1.00`).
- Total evaluation events, counts distribution (`passed`, `accepted`, `rework`, `failed`), and average completion time (`avg_sec`).
- Models with low scores are automatically skipped during routing.

### ⏳ 6) Quota Limits & Instant Clear
- Displays quota groups resting due to rate limits (429) or missing sign-ins, and their reset time.
- **"모두 즉시 해제"** button or individual clear buttons to clear cooldowns immediately for testing.

### 🎨 7) Theme & Live Auto-refresh
- Auto-refreshes every 5 seconds with a visual countdown progress bar.
- Pause/Resume button to freeze the view while reading logs.
- Dark / Light / Auto (system preference) theme toggle.

---

## 2. Host Agent Guidance

When an operator asks how to use the dashboard, where it is, or how to inspect their jobs:
1. Provide the clickable link: **`http://127.0.0.1:8790/`**.
2. Explain that they can view reports, inspect colorized diffs, and land or rework jobs with a single click.
3. If they need to log into `agy`, tell them they can paste the Google response code directly into the dashboard's code input box.
4. If the dashboard is not running, call `shuttle_dashboard()` to launch it.
