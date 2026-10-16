#!/usr/bin/env python3
"""MCP server (stdio) exposing delegate.py to an orchestrating agent. Requires `pip install mcp` (1.x or 2.x)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegate as d  # noqa: E402

try:   # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server  # noqa: E402
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server  # noqa: E402

mcp = _Server("delegate")


@mcp.tool()
def delegate_clis() -> list:
    """List the worker CLIs this host can delegate to, whether each is installed, and its permission profile."""
    return d.clis()


@mcp.tool()
def delegate_start(task: str, repo: str, difficulty: str = "normal", check: str = "", title: str = "",
                   cli: str = "", model: str = "", base: str = "", timeout_sec: int = d.DEFAULT_TIMEOUT,
                   chain: List[str] = [], fallback: bool = True, auto_fix_rounds: int = 2) -> dict:
    """Start a worker CLI on a task in a fresh git worktree of `repo` (branch delegate/<id> from `base`, default
    HEAD). Returns at once with the job id; the worker runs in the background. `task` is the full brief: goal,
    files/area, constraints, acceptance criteria.
    Routing (cheapest first): leave `cli` empty and set `difficulty`: easy | normal start on opencode's free models
    and escalate to agy (Gemini, then its Claude group) when they cannot make `check` pass; hard starts on agy's
    Claude group. Name `cli` only to force one worker (claude spends Claude quota; codex/grok may be disabled).
    When a brain hits a quota/rate limit or is not signed in, the job moves on by itself: the same CLI's other
    models (agy: Gemini group, then Claude/GPT group) and accounts, then other CLIs (unless fallback=false).
    `chain` overrides the order explicitly: ["agy:gemini-3.8-flash-high", "codex@work", "grok"]
    (cli[:model][@account]). See delegate_chain for what would be tried.
    `check`: an acceptance command (e.g. "py -3 -m pytest -q tests/test_x.py") the runner executes in the worktree
    after each round; while it fails, the worker gets its output and retries, up to auto_fix_rounds, without you.
    Always give one when the task is testable: it is the cheapest review there is."""
    return d.start(cli, task, repo=repo, title=title, model=model, base=base, timeout=timeout_sec,
                   chain=chain or None, fallback=fallback, check=check, auto_fix=auto_fix_rounds,
                   difficulty=difficulty)


@mcp.tool()
def delegate_compete(clis: List[str], task: str, repo: str, title: str = "", base: str = "",
                     timeout_sec: int = d.DEFAULT_TIMEOUT, check: str = "", auto_fix_rounds: int = 2) -> list:
    """Give the same task to several CLIs at once, each in its own worktree, to compare and land the best."""
    return d.start_many(clis, task, repo=repo, title=title, base=base, timeout=timeout_sec, check=check,
                        auto_fix=auto_fix_rounds)


@mcp.tool()
def delegate_auth(cli: str = "", open_login: bool = True) -> list:
    """Sign-in state of every account of every installed CLI (or one CLI). Each one not signed in gets its own
    sign-in window opened on this machine (open_login; claude only when named) and carries the `login` command:
    tell the operator to finish it there. opencode needs no sign-in (free built-in models). Jobs open the window
    themselves when a worker turns out signed out, and wait for the sign-in when nothing else in the chain can work."""
    return d.auth(cli, open_login)


@mcp.tool()
def delegate_dashboard(open_browser: bool = True) -> dict:
    """Open the local management dashboard on this machine (starts it if needed): sign-in state with a button that
    opens each CLI's sign-in window, resting quota groups (clear them), opencode's free models, the difficulty
    chains, and the jobs (cancel / discard). Give the operator the returned url."""
    return d.dashboard(open_browser=open_browser)


@mcp.tool()
def delegate_login(cli: str, account: str = "default") -> dict:
    """Open the CLI's own interactive sign-in in a new console window on this machine, for `account` (a new
    account directory is created for a non-default name, so several logins of one CLI can work in turn). The
    operator completes it (browser / device code); you never type credentials. Ask the operator before calling."""
    return d.login(cli, account)


@mcp.tool()
def delegate_agy_switch(confirm_logout: bool = False) -> dict:
    """Swap agy to another Google account (more agy quota) through agy's own OAuth sign-in. Signs the CURRENT agy
    account out first, so ask the operator and pass confirm_logout=true only after they agree. Returns the sign-in
    `url`: give it to the operator to open; they sign in with the other account and paste back the response code,
    which you pass to delegate_agy_code. Quota rests are per email, so the new account's quota is usable at once."""
    if not confirm_logout:
        return {"state": "not_started", "why": "this signs agy's current account out; ask the operator, then call "
                "again with confirm_logout=true", "email_now": d.B.agy_email()}
    import agy_login
    return agy_login.begin()


@mcp.tool()
def delegate_agy_code(code: str) -> dict:
    """Finish delegate_agy_switch: type the response code the operator copied from the Google sign-in page into
    agy. Returns the signed-in email. The code is used once and never stored."""
    import agy_login
    return agy_login.finish(code)


@mcp.tool()
def delegate_ratings() -> list:
    """How each brain (cli:model) has done per difficulty: score 0..1, event counts (passed, escalated, failed,
    accepted, rework, rejected), mean worker seconds. Routing already skips brains proven weak at a difficulty;
    read this to choose `difficulty` well and to tell the operator which workers earn their keep."""
    return d.RT.table()


@mcp.tool()
def delegate_chain(difficulty: str = "normal", cli: str = "", model: str = "", fallback: bool = True) -> list:
    """The ordered brains (cli:model@account) a job would try — for a difficulty tier, or led by `cli` — each with
    its quota group and any rest (quota, missing sign-in, blocked) still on it."""
    return d.chain_view(cli, model, None, fallback, "" if cli else difficulty)


@mcp.tool()
def delegate_limits(clear: str = "") -> list:
    """Quota groups currently resting and until when. clear="*" forgets all rests; clear="<text>" forgets those
    whose group contains it (e.g. after the operator signed in or a quota was raised)."""
    if clear:
        d.B.clear_limits("" if clear == "*" else clear)
    return d.B.limits()


@mcp.tool()
def delegate_wait(ids: List[str], timeout_sec: int = 300, any_finished: bool = False) -> dict:
    """Block until the given jobs finish (or any one, with any_finished), at most timeout_sec (cap 600).
    `finished: false` means still running: call again."""
    return d.wait(ids, min(int(timeout_sec), 600), any_finished)


@mcp.tool()
def delegate_status(id: str) -> dict:
    """One job's state: running | done | failed | lost | cancelled | merged | discarded."""
    return d.status(id)


@mcp.tool()
def delegate_list(active_only: bool = True, limit: int = 30) -> list:
    """Recent delegation jobs, newest first."""
    return d.list_jobs(active_only, limit)


@mcp.tool()
def delegate_result(id: str, paths: List[str] = [], max_diff_chars: int = 0) -> dict:
    """A finished job, lean: the worker's REPORT.md, diffstat, commits, short log tail, timings, fallbacks. No diff by
    default, to save your tokens: pass `paths` for the diff of specific files (from the diffstat), or
    max_diff_chars>0 for the whole diff up to that size. Prefer reading files in the `worktree` path for context.
    For a review job: the reviewer's `answer`."""
    return d.result(id, max_diff_chars, paths or None)


@mcp.tool()
def delegate_check(id: str, command: str, timeout_sec: int = 1800) -> dict:
    """Run a command (tests, lint, build) in a finished job's worktree; returns exit code and a short tail only.
    Cheaper than running it yourself and reading the full output."""
    return d.check(id, command, timeout_sec)


@mcp.tool()
def delegate_followup(id: str, feedback: str, cli: str = "", model: str = "") -> dict:
    """Send a finished work job back for another round in the same worktree with your review feedback (concrete,
    numbered, file:line). Optionally switch to another cli/model for this round."""
    return d.followup(id, feedback, cli, model)


@mcp.tool()
def delegate_review(id: str, cli: str, focus: str = "", model: str = "", chain: List[str] = []) -> dict:
    """Ask another CLI for a second-opinion review of a work job's diff (read-only; it never sees the worktree).
    Returns a review job id; wait for it and read its stdout_tail via delegate_result."""
    return d.review(id, cli, model, focus, chain=chain or None)


@mcp.tool()
def delegate_merge(id: str, mode: str = "squash", message: str = "", keep_worktree: bool = False) -> dict:
    """Land a reviewed, finished job onto the repo's checked-out branch: squash (one commit, default) | ff | merge.
    Refuses when the main working tree has uncommitted tracked changes. Removes the worktree and branch unless
    keep_worktree. Only call after you reviewed the diff and the operator's landing rules allow it."""
    return d.merge(id, mode, message, keep_worktree)


@mcp.tool()
def delegate_cancel(id: str) -> dict:
    """Stop a running job (kills the worker process tree). The worktree stays for inspection."""
    return d.cancel(id)


@mcp.tool()
def delegate_discard(id: str) -> dict:
    """Throw a job's work away: stops it if running, removes its worktree and branch."""
    return d.discard(id)


if __name__ == "__main__":
    mcp.run()
