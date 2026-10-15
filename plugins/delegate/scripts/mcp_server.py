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
def delegate_start(cli: str, task: str, repo: str, title: str = "", model: str = "", base: str = "",
                   timeout_sec: int = d.DEFAULT_TIMEOUT, chain: List[str] = [], fallback: bool = True) -> dict:
    """Start a worker CLI on a task in a fresh git worktree of `repo` (branch delegate/<id> from `base`, default
    HEAD). Returns at once with the job id; the worker runs in the background. `task` is the full brief: goal,
    files/area, constraints, acceptance criteria. `cli`: codex | claude | grok | agy | opencode (see delegate_clis).
    When a brain hits a quota/rate limit or is not signed in, the job moves on by itself: the same CLI's other
    models (agy: Gemini group, then Claude/GPT group) and accounts, then other CLIs (unless fallback=false).
    `chain` overrides the order explicitly: ["agy:gemini-3.8-flash-high", "codex@work", "grok"]
    (cli[:model][@account]). See delegate_chain for what would be tried."""
    return d.start(cli, task, repo=repo, title=title, model=model, base=base, timeout=timeout_sec,
                   chain=chain or None, fallback=fallback)


@mcp.tool()
def delegate_compete(clis: List[str], task: str, repo: str, title: str = "", base: str = "",
                     timeout_sec: int = d.DEFAULT_TIMEOUT) -> list:
    """Give the same task to several CLIs at once, each in its own worktree, to compare and land the best."""
    return d.start_many(clis, task, repo=repo, title=title, base=base, timeout=timeout_sec)


@mcp.tool()
def delegate_auth(cli: str = "") -> list:
    """Sign-in state of every account of every installed CLI (or one CLI). Entries with logged_in=false carry the
    exact `login` command to give the operator. Run this before a big fan-out, and when a job reports needs_login."""
    return d.auth(cli)


@mcp.tool()
def delegate_login(cli: str, account: str = "default") -> dict:
    """Open the CLI's own interactive sign-in in a new console window on this machine, for `account` (a new
    account directory is created for a non-default name, so several logins of one CLI can work in turn). The
    operator completes it (browser / device code); you never type credentials. Ask the operator before calling."""
    return d.login(cli, account)


@mcp.tool()
def delegate_chain(cli: str, model: str = "", fallback: bool = True) -> list:
    """The ordered brains (cli:model@account) a job started with this cli/model would try, each with its quota
    group and any rest (quota or missing sign-in) still on it."""
    return d.chain_view(cli, model, None, fallback)


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
def delegate_result(id: str, max_diff_chars: int = d.DEFAULT_MAX_DIFF) -> dict:
    """A finished job's output for review: the worker's REPORT.md, commits, diffstat, the diff against base, and
    the tail of its stdout/stderr. For a review job: the reviewer's answer (stdout_tail). The worktree path is in
    the result; read files there directly when the diff is truncated or you need context."""
    return d.result(id, max_diff_chars)


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
