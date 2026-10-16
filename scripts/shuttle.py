#!/usr/bin/env python3
"""Shuttle coding tasks to other agent CLIs in isolated git worktrees, then review and land the result.

The orchestrating agent is the director: it writes a brief, hands it to a worker CLI (codex, claude, grok,
agy, opencode) running headless in its own worktree, waits, reviews the diff, asks for rework or lands it.

The same functions back the MCP server (mcp_server.py) and this command line:

    shuttle.py clis
    shuttle.py start --cli codex --repo . --title "..." --task-file brief.md [--model M] [--timeout 3600]
    shuttle.py list | status <id> | wait <id>... [--timeout 600] | result <id> [--max-diff N]
    shuttle.py followup <id> --feedback-file f.md | review <id> --cli claude | cancel <id>
    shuttle.py merge <id> [--mode squash|ff|merge] [--message M] [--keep] | discard <id>

State lives under $SHUTTLE_HOME (default ~/.shuttle): jobs/<id>/job.json, logs, and worktrees/<id>.
Worker command lines default to each CLI's sandboxed / edit-only permission mode. To change them, the operator
writes $SHUTTLE_HOME/providers.json (see README); this script never widens them by itself.
Modelled on PrivateEngine's worktree runner (engine/dev/worktree_runner.py), without its ticket governance.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brains as B  # noqa: E402
import ratings as RT  # noqa: E402

HOME = Path(os.environ.get("SHUTTLE_HOME") or Path.home() / ".shuttle")
JOBS = HOME / "jobs"
WORKTREES = HOME / "worktrees"
IS_WIN = os.name == "nt"
BRIEF_DIR = ".shuttle"             # inside each worktree, git-excluded: TASK.md in, REPORT.md out
DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_DIFF = 60000   # what a second-opinion reviewer gets
RESULT_DIFF = 0            # what result() returns by default: no diff (the director's tokens are the scarce ones)
TAIL = 4000

# Each CLI headless, in its own restricted mode. `work`: may edit files in its cwd (the worktree). `review`:
# read-only; the reviewer also runs in a separate room holding only the review brief, never the worktree.
# `extra` flags go before a trailing "-p" so the prompt stays last. `author`: git identity of the runner's commit.
PROVIDERS: Dict[str, Dict] = {
    "codex": {"work": ["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check"],
              "review": ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"],
              "model_flag": "-m", "effort_flag": ["-c", "model_reasoning_effort={}"], "author": ("Codex", "codex@localhost"),
              "note": "OpenAI Codex CLI; workspace-write sandbox, can run tests"},
    "claude": {"work": ["claude", "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Glob,Grep", "-p"],
               "review": ["claude", "--allowedTools", "Read,Grep,Glob", "-p"],
               "model_flag": "--model", "effort_flag": ["--effort", "{}"], "author": ("Claude Code", "noreply@anthropic.com"),
               "note": "Claude Code, fresh context; edits only (no shell) unless providers.json allows more"},
    "grok": {"work": ["grok", "--permission-mode", "acceptEdits", "-p"],
             "review": ["grok", "--permission-mode", "plan", "-p"],
             "model_flag": "-m", "author": ("Grok", "grok@localhost"), "note": "xAI Grok Build; accept-edits mode"},
    "agy": {"work": ["agy-real", "--sandbox", "--mode", "accept-edits", "-p"],
            "review": ["agy-real", "--sandbox", "--mode", "plan", "-p"],
            "model_flag": "--model", "workdir_flag": "--add-dir", "author": ("agy", "agy@localhost"),
            "note": "Antigravity (Gemini); sandboxed, accept-edits mode"},
    # --standalone: without it `opencode run` talks to the shared background service, which works in that
    # service's folder, not the worktree (observed: it wrote into another repo's root).
    "opencode": {"work": ["opencode", "run", "--standalone"], "review": ["opencode", "run", "--standalone", "--agent", "plan"],
                 "model_flag": "-m", "author": ("opencode", "opencode@localhost"),
                 "note": "opencode; uses its own configured permissions"},
}


def load_overrides() -> None:
    """Operator-owned $SHUTTLE_HOME/providers.json: per-CLI keys replace the defaults above, new CLIs are added."""
    p = HOME / "providers.json"
    if not p.exists():
        return
    for name, spec in json.loads(p.read_text(encoding="utf-8")).items():
        base = dict(PROVIDERS.get(name, {"model_flag": "--model", "note": "custom"}))
        base.update(spec)
        base["author"] = tuple(base.get("author") or (name, name + "@localhost"))
        PROVIDERS[name] = base


load_overrides()

WORK_PROMPT = ("Read the task brief at {brief} and carry it out in the current directory. Follow it exactly, "
               "including writing the report it asks for at the end.")
REVIEW_PROMPT = "Read the review brief at {brief} and answer as it asks. Do not modify any files."


# ------------------------------------------------------------------ helpers

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = False, timeout: int = 120) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed (%s): %s" % (" ".join(cmd[:4]), p.returncode, (p.stderr or p.stdout).strip()[-800:]))
    return p


def git(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    return run(["git", *args], cwd=repo, check=check, timeout=timeout).stdout.strip()


def tail(text: str, n: int = TAIL) -> str:
    return text if len(text) <= n else "...(truncated)...\n" + text[-n:]


def read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    if IS_WIN:
        run(["taskkill", "/T", "/F", "/PID", str(pid)])
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
        except OSError:
            pass


# ------------------------------------------------------------------ job state

def job_dir(jid: str) -> Path:
    return JOBS / jid


def load(jid: str) -> Dict:
    p = job_dir(jid) / "job.json"
    if not p.exists():
        raise KeyError("no such shuttle job: %s" % jid)
    return json.loads(p.read_text(encoding="utf-8"))


def save(job: Dict) -> Dict:
    d = job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "job.json")
    return job


def refresh(job: Dict) -> Dict:
    """A job marked running whose runner died without recording an end is reported as `lost`."""
    if job.get("status") == "running" and not pid_alive(job.get("runner_pid")) and not pid_alive(job.get("agent_pid")):
        job = load(job["id"])  # the runner may have finished between the two reads
        if job.get("status") == "running":
            job.update(status="lost", finished=now(), error="runner process is gone without a result")
            save(job)
    return job


def summary(job: Dict) -> Dict:
    keys = ("id", "kind", "cli", "model", "account", "title", "status", "round", "repo", "worktree", "branch", "base",
            "started", "agent_finished", "finished", "exit_code", "elapsed_sec", "agent_sec", "commit_sec", "error",
            "needs_login", "group", "parent")
    out = {k: job[k] for k in keys if job.get(k) not in (None, "", [])}
    if job.get("check_result"):
        c = job["check_result"]
        out["check"] = "passed" if c["passed"] else "failed (exit %s) after %d automatic fix round(s)" % (
            c["exit_code"], job.get("auto_used", 0))
    if job.get("phase"):
        out["phase"] = job["phase"]
    if job.get("difficulty"):
        out["difficulty"] = job["difficulty"]
    if job.get("routing_notes"):
        out["routing"] = job["routing_notes"]
    if job.get("warnings"):
        out["warnings"] = job["warnings"]
    if job.get("escalations"):
        out["escalated"] = ["%s -> %s (round %s)" % (e["from"], e["to"], e["round"]) for e in job["escalations"]]
    if job.get("commit_error"):   # never only inside result(): a refused commit must show in status and wait
        out["commit_error"] = job["commit_error"].strip().splitlines()[-1][:300] + " (see shuttle_result)"
    tried = [a for a in job.get("attempts", []) if a.get("round") == job.get("round") and a.get("outcome") in ("auth", "unavailable", "blocked", "stalled")]
    if tried:   # brains this round skipped past (quota / sign-in)
        out["fell_back_from"] = ["%s (%s until %s)" % (a["brain"], a["outcome"], a.get("rested_until", "?")) for a in tried]
    return out


# ------------------------------------------------------------------ command lines

def with_flags(argv: List[str], flags: List[str]) -> List[str]:
    argv = list(argv)
    at = len(argv) - 1 if argv and argv[-1] == "-p" else len(argv)
    argv[at:at] = flags
    return argv


def command(cli: str, kind: str, cwd: Path, model: str, prompt: str, effort: str = "") -> List[str]:
    spec = PROVIDERS[cli]
    flags: List[str] = []
    if kind == "work" and spec.get("workdir_flag"):
        flags += [spec["workdir_flag"], str(cwd)]
    if model:
        flags += [spec["model_flag"], model]
    if cli == "agy" and kind == "work" and B.config().get("agy_yolo"):   # opt-in; see brains.DEFAULTS agy_yolo
        flags += ["--dangerously-skip-permissions"]
    if effort and spec.get("effort_flag"):   # agy has none: its effort is the model's -low/-medium/-high variant
        flags += [f.format(effort) for f in spec["effort_flag"]]
    argv = with_flags(spec[kind], flags) + [prompt]
    exe = shutil.which(argv[0])
    if not exe:
        raise RuntimeError("%s CLI is not installed or not on PATH" % argv[0])
    argv[0] = exe
    return argv


DIRECTOR_ENV_PREFIXES = ("CLAUDE", "CODEX_", "ANTIGRAVITY_", "GROK_", "OPENCODE_")
DIRECTOR_ENV_EXACT = {"PLUGIN_ROOT", "PLUGIN_DATA"}


def base_env() -> Dict[str, str]:
    """This process's environment minus the orchestrator's own session: a worker uses its own CLI login."""
    env = dict(os.environ)
    hosted = "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH" in env   # the desktop app routes its own session's auth
    for k in list(env):
        if k.startswith(DIRECTOR_ENV_PREFIXES) or k in DIRECTOR_ENV_EXACT or (hosted and k == "ANTHROPIC_BASE_URL"):
            env.pop(k, None)
    return env


# opencode's default for anything outside the project is "ask", which headless `opencode run` turns into a
# rejection that ends the whole run (exit 1, nothing done). "deny" instead hands the model an error and it carries
# on (observed: a model that went to read ~/.agents/AGENTS.md aborted under "ask", finished the task under "deny").
# Merged over the operator's own opencode config; providers.json "env" can replace it.
OPENCODE_HEADLESS = {"permission": {"external_directory": "deny", "question": "deny"}}


def agent_env(cli: str, account: str = "default") -> Dict[str, str]:
    env = base_env()
    env.update(B.account_env(cli, account))
    if cli == "opencode":
        env.setdefault("OPENCODE_CONFIG_CONTENT", json.dumps(OPENCODE_HEADLESS))
    env.update({k: str(v) for k, v in (PROVIDERS[cli].get("env") or {}).items()})
    name, email = PROVIDERS[cli]["author"]
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
               SHUTTLE_WORKER="1")
    return env


# ------------------------------------------------------------------ briefs

def work_brief(job: Dict, task: str, feedback: List[str], handover: str = "") -> str:
    if handover:   # an earlier brain of this round stopped (quota, sign-in); this one takes over its worktree
        note = ("\n\nNote: another worker started this round and was stopped (%s) before finishing. Its partial, "
                "uncommitted changes may be in the working tree: check `git status` / `git diff`, keep what is right, "
                "and finish the task." % handover)
    else:
        note = ""
    rounds = note + "".join("\n\n## Rework round %d — reviewer feedback (address every point)\n\n%s" % (i + 2, f)
                     for i, f in enumerate(feedback))
    if feedback:
        rounds = ("\n\nThis is a rework round. Your earlier rounds' changes are already in this worktree: everything "
                  "in `git diff %s` (the task's base commit) is this task's own work, not pre-existing code, and "
                  "the reviewer may ask you to undo any of it." % job["base"][:12]) + rounds
    return """# Shuttled task: {title}

You are a worker agent. A director agent wrote this brief, will review your diff line by line, and decides whether
it lands. You work alone in a git worktree of `{repo_name}` on branch `{branch}`.

## Task

{task}{rounds}

## Rules

- Work only inside the current directory (the worktree). Do not touch any other path, and do not push.
- Do not commit: leave your changes in the working tree. The runner commits them for you.
- Follow the repository's own agent instructions (AGENTS.md, CLAUDE.md, CONTRIBUTING, etc.) when present.
- Keep the change to what the task asks. No drive-by refactors, no unrelated formatting.
- Run the tests or checks relevant to your change if your tools allow it, and fix what you broke.
- If the task is ambiguous or impossible as written, do the safe part and say so in the report instead of guessing.

## Report (required)

When done, write `{brief_dir}/REPORT.md` (this folder is ignored by git) with these sections:
`## Summary`, `## Files changed`, `## Tests run` (commands and results, or why none), `## Risks / open questions`.
""".format(title=job["title"], repo_name=Path(job["repo"]).name, branch=job["branch"], task=task.strip(),
           rounds=rounds, brief_dir=BRIEF_DIR)


def review_brief(parent: Dict, diff: str, report: str, focus: str) -> str:
    return """# Review request: {title}

You are a code reviewer. Another agent made the change below for this task. Review it; do not modify files.

## Task given to the worker

{task}

## Worker's report

{report}

## Diff (base {base} -> {branch})

```diff
{diff}
```
{focus}
## Answer format

Start with one line `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Then list findings, most severe first, each
as `- [severity] file:line — problem — suggested fix` (severity: blocker | major | minor | nit). Only real problems:
correctness, missed requirements, broken tests, security, scope creep. End with a short overall note.
""".format(title=parent["title"], task=read(job_dir(parent["id"]) / "task.md").strip(),
           report=report.strip() or "(no report)", base=parent["base"][:10], branch=parent["branch"], diff=diff,
           focus=("\n## Reviewer focus\n\n%s\n" % focus.strip()) if focus.strip() else "")


# ------------------------------------------------------------------ public API

def clis() -> List[Dict]:
    out = []
    for name, spec in PROVIDERS.items():
        exe = shutil.which(spec["work"][0])
        out.append({"cli": name, "installed": bool(exe), "enabled": name not in B.disabled(), "path": exe or "",
                    "note": spec.get("note", "")})
    return out


def new_id(cli: str) -> str:
    return "%s-%s-%s" % (datetime.now().strftime("%m%d-%H%M%S"), cli, secrets.token_hex(2))


def exclude_brief_dir(repo: Path) -> None:
    """The brief folder and tool caches (brains.json `exclude`) stay out of every commit in this repo's worktrees."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    excl = common / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    lines = read(excl).splitlines()
    want = ["/%s/" % BRIEF_DIR] + [x for x in B.config().get("exclude", []) if x != "/%s/" % BRIEF_DIR]
    missing = [x for x in want if x not in lines]
    if missing:
        with excl.open("a", encoding="utf-8") as f:
            f.write(("\n" if lines and lines[-1] else "") + "".join(x + "\n" for x in missing))


def installed(cli: str) -> bool:
    return cli in PROVIDERS and bool(shutil.which(PROVIDERS[cli]["work"][0]))


def make_chain(cli: str = "", model: str = "", chain: Optional[List[str]] = None, fallback: bool = True,
               difficulty: str = "") -> List[Dict]:
    """The brains a job may use, in order: an explicit chain of 'cli[:model][@account]'; else, when `cli` is given,
    its models x accounts then its fallback CLIs'; else the difficulty tier (brains.json `tiers`)."""
    if chain:
        brains = [B.parse(s) for s in chain]
    elif not cli:
        brains = B.tier_chain(difficulty or B.config().get("default_difficulty") or "normal", installed)
    else:
        if cli not in PROVIDERS:
            raise ValueError("unknown cli %r; choose from %s" % (cli, ", ".join(PROVIDERS)))
        brains = B.chain_for(cli, model, fallback, installed, difficulty)
    bad = sorted({b["cli"] for b in brains if b["cli"] not in PROVIDERS})
    if bad:
        raise ValueError("unknown cli in chain: %s" % ", ".join(bad))
    off = sorted({b["cli"] for b in brains} & B.disabled())
    if off:
        raise ValueError("%s disabled (brains.json `disabled`); remove it there to use it" % ", ".join(off))
    brains = B.expand([b for b in brains if installed(b["cli"])])
    effort = (B.config().get("effort") or {}).get(difficulty or B.config().get("default_difficulty") or "normal", "")
    for b in brains:   # CLIs that take reasoning effort as a flag get the difficulty's
        if effort and PROVIDERS[b["cli"]].get("effort_flag") and not b.get("effort"):
            b["effort"] = effort
    if not brains:
        raise RuntimeError("none of the chain's CLIs is installed")
    return brains


def start(cli: str, task: str, repo: str = ".", title: str = "", model: str = "", base: str = "",
          timeout: int = DEFAULT_TIMEOUT, group: str = "", chain: Optional[List[str]] = None,
          fallback: bool = True, check: str = "", auto_fix: int = 2, difficulty: str = "") -> Dict:
    if not task.strip():
        raise ValueError("task brief is empty")
    difficulty = difficulty or B.config().get("default_difficulty") or "normal"
    brains = make_chain(cli, model, chain, fallback, difficulty)
    notes: List[str] = []
    if not cli and not chain:   # the tier's order, minus brains the ratings show are weak at this difficulty
        brains, notes = RT.route(brains, difficulty)
    first = brains[0]
    root = Path(git(Path(repo).resolve(), "rev-parse", "--show-toplevel"))
    base_sha = git(root, "rev-parse", base or "HEAD")
    jid = new_id(first["cli"])
    wt, branch = WORKTREES / jid, "shuttle/%s" % jid
    WORKTREES.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", branch, str(wt), base_sha, timeout=600)
    exclude_brief_dir(root)
    job = {"id": jid, "kind": "work", "cli": first["cli"], "model": first["model"], "account": first["account"],
           "title": title or task.strip().splitlines()[0][:80], "status": "starting", "round": 1, "repo": str(root),
           "worktree": str(wt), "branch": branch, "base": base_sha,
           "base_ref": git(root, "rev-parse", "--abbrev-ref", "HEAD"), "timeout": int(timeout), "created": now(),
           "group": group, "feedback": [], "chain": brains, "attempts": [], "check": check.strip(),
           "auto_fix": max(0, int(auto_fix)), "auto_used": 0, "chain_pos": 0,
           "difficulty": difficulty, "routing_notes": notes}
    d = job_dir(jid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(task, encoding="utf-8")
    save(job)
    out = launch(job)
    url = show_dashboard()
    if url:
        out["dashboard"] = url
    return out


def show_dashboard() -> str:
    """Bring the dashboard up for a new job; open the browser only when it was not running yet. "" when off."""
    if not B.config().get("dashboard_on_start", True) or os.environ.get("SHUTTLE_NO_DASHBOARD"):
        return ""
    try:
        up = dashboard(open_browser=False)
        if not up["already_running"]:
            import webbrowser
            webbrowser.open(up["url"])
        return up["url"]
    except Exception:  # noqa: BLE001 - the job matters, the dashboard does not
        return ""


def start_many(clis_: List[str], task: str, **kw) -> List[Dict]:
    """The same task to several CLIs at once (compete mode); each gets its own worktree. No cross-CLI fallback
    here (accounts and models of the same CLI still are), so the candidates stay distinct."""
    group = kw.pop("group", "") or "g-" + secrets.token_hex(3)
    return [start(c, task, group=group, fallback=False, **kw) for c in clis_]


def launch(job: Dict) -> Dict:
    """Spawn the background runner for the job's current round."""
    d = job_dir(job["id"])
    kw: Dict = {"creationflags": 0x00000200 | 0x08000000} if IS_WIN else {"start_new_session": True}
    job.update(status="running", started=now(), finished="", exit_code=None, error="", agent_pid=None)
    job.pop("needs_login", None)
    job.pop("phase", None)
    save(job)
    with open(d / "runner.log", "ab") as log:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_run", job["id"]], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, close_fds=True, **kw)
    job["runner_pid"] = p.pid
    save(job)
    return summary(job)


def _progress(paths: List[Path]) -> tuple:
    """A cheap fingerprint of activity: newest mtime and total size over these files and folders (shallow walk,
    .git skipped). Any write by the worker changes it."""
    newest, total = 0.0, 0
    for root in paths:
        try:
            if root.is_file():
                st = root.stat()
                newest, total = max(newest, st.st_mtime), total + st.st_size
                continue
            for dirpath, dirnames, files in os.walk(root):
                dirnames[:] = [x for x in dirnames if x not in (".git", "node_modules", "__pycache__")]
                for f in files:
                    try:
                        st = os.stat(os.path.join(dirpath, f))
                    except OSError:
                        continue
                    newest, total = max(newest, st.st_mtime), total + st.st_size
        except OSError:
            continue
    return newest, total


def _watch_paths(b: Dict, cwd: Path, logs: List[Path]) -> List[Path]:
    """Where a running brain leaves traces: its logs, the worktree, and the CLI's own log folder (agy prints
    nothing until it is done, but writes its log as it works)."""
    out = logs + [cwd]
    if b["cli"] == "agy":
        home = Path(B.account_env("agy", b["account"]).get("HOME") or B.USER_HOME)
        out.append(home / ".gemini" / "antigravity-cli" / "log")
    return out


def _spawn(jid: str, argv: List[str], cwd: Path, b: Dict, stem: str, timeout: int) -> tuple:
    """Run one brain to its end. (exit code, error text). Stopped early when it makes no progress for
    `stall_sec` (error starts with "stalled")."""
    d = job_dir(jid)
    with open(d / ("stdout.%s.log" % stem), "wb") as out, open(d / ("stderr.%s.log" % stem), "wb") as err:
        kw = {"creationflags": 0x08000000} if IS_WIN else {"start_new_session": True}
        # PWD too: opencode takes its project folder from $PWD, not the process cwd (observed: an inherited PWD
        # made it work in the orchestrator's repo instead of the worktree)
        p = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             env={**agent_env(b["cli"], b["account"]), "PWD": str(cwd)}, **kw)
        job = load(jid)
        job["agent_pid"] = p.pid
        save(job)
        stall = int(B.config().get("stall_sec") or 0)
        watch = _watch_paths(b, cwd, [d / ("stdout.%s.log" % stem), d / ("stderr.%s.log" % stem)])
        start = last_change = time.time()
        seen = _progress(watch)
        while True:
            try:
                return p.wait(timeout=min(10, stall / 3 if stall else 10, max(1, timeout - (time.time() - start)))), ""
            except subprocess.TimeoutExpired:
                pass
            now_ = time.time()
            if now_ - start >= timeout:
                kill_tree(p.pid)
                p.wait(30)
                return -1, "timed out after %ss" % timeout
            cur = _progress(watch)
            if cur != seen:
                seen, last_change = cur, now_
            elif stall and now_ - last_change >= stall:
                kill_tree(p.pid)
                p.wait(30)
                return -1, "stalled: no output, file or log change for %ds" % int(now_ - last_change)


def run_check(job: Dict, command: str = "", timeout: int = 1800) -> Dict:
    """Run an acceptance command (tests, lint, build) in the job's worktree. No model involved: exit code + tail."""
    command = command or job.get("check", "")
    t0 = time.time()
    try:
        p = subprocess.run(command, shell=True, cwd=job["worktree"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env={**base_env(), "PWD": str(job["worktree"])}, stdin=subprocess.DEVNULL)
        code, text = p.returncode, (p.stdout + "\n" + p.stderr).strip()
    except subprocess.TimeoutExpired:
        code, text = -1, "check timed out after %ss" % timeout
    return {"command": command, "exit_code": code, "passed": code == 0, "sec": round(time.time() - t0, 1),
            "tail": tail(text, 3000), "at": now()}


def _run(jid: str) -> int:
    """The background runner. Runs a round; when the job has an acceptance `check`, runs it, and while it fails
    and automatic fix rounds remain, sends the worker its output as a new round -- no director tokens spent."""
    _round(jid)
    while True:
        job = load(jid)
        if job["kind"] != "work" or job.get("status") != "done" or not job.get("check"):
            return 0
        job["phase"] = "checking"
        save(job)
        chk = run_check(job)
        job = load(jid)
        if job.get("status") != "done":     # cancelled meanwhile
            return 0
        job.pop("phase", None)
        job["check_result"] = chk
        job.setdefault("checks", []).append({k: chk[k] for k in ("exit_code", "sec", "at")} | {"round": job["round"]})
        diff_, bk = job.get("difficulty", ""), job.get("brain_key", "")
        if chk["passed"]:
            save(job)
            RT.record(bk, diff_, "passed", jid, auto_rounds=job.get("auto_used", 0), agent_sec=job.get("agent_sec"))
            return 0
        failed_note = ("Automatic acceptance check failed. Command: `%s` (exit %s). Make it pass without weakening or "
                       "skipping the check. Output (tail):\n\n```\n%s\n```" % (chk["command"], chk["exit_code"], chk["tail"]))
        if job.get("auto_used", 0) >= job.get("auto_fix", 0):
            nxt = next_brain(job)
            if nxt is None:                 # nobody stronger left: the director decides
                save(job)
                RT.record(bk, diff_, "failed_check", jid, auto_rounds=job.get("auto_used", 0))
                return 0
            RT.record(bk, diff_, "escalated", jid, auto_rounds=job.get("auto_used", 0))
            job.setdefault("escalations", []).append({"from": job.get("brain_label", ""), "to": B.label(job["chain"][nxt]),
                                                      "round": job["round"], "why": "check still failing"})
            job.update(chain_pos=nxt, auto_used=0)
            failed_note = ("The previous worker (%s) could not make the acceptance check pass; you take over its "
                           "worktree. Keep what is right, fix or redo the rest. " % job.get("brain_label", "?")) + failed_note
        else:
            job["auto_used"] = job.get("auto_used", 0) + 1
        job["feedback"] = job.get("feedback", []) + [failed_note]
        job["round"] = job.get("round", 1) + 1
        job.update(status="running", started=now(), finished="", exit_code=None, error="")
        save(job)
        _round(jid)


def main_tree_state(job: Dict) -> set:
    """`git status --porcelain` lines of the repo's main working tree (the operator's), to catch escaped writes."""
    return set(git(Path(job["repo"]), "status", "--porcelain", check=False).splitlines())


def next_brain(job: Dict) -> Optional[int]:
    """Index of the first brain after the one that did this job's work, or None."""
    pos = job.get("brain_pos", job.get("chain_pos", 0))
    return pos + 1 if pos + 1 < len(job["chain"]) else None


def _round(jid: str) -> int:
    """One round of one job, down the job's chain of brains. A brain that is out (quota,
    limit, outage) or not signed in is rested for its quota group and the next one takes over the same worktree."""
    job = load(jid)
    d, t0, rnd, handover = job_dir(jid), time.time(), job.get("round", 1), ""
    work = job["kind"] == "work"
    cfg = B.config()
    try:
        start_at = job.get("chain_pos", 0)
        for pos, b in enumerate(job["chain"]):
            if pos < start_at or B.resting(b):
                continue
            if work:
                cwd = Path(job["worktree"])
                (cwd / BRIEF_DIR).mkdir(exist_ok=True)
                (cwd / BRIEF_DIR / "TASK.md").write_text(
                    work_brief(job, read(d / "task.md"), job.get("feedback", []), handover), encoding="utf-8")
                (cwd / BRIEF_DIR / "REPORT.md").unlink(missing_ok=True)
                prompt = WORK_PROMPT.format(brief="%s/TASK.md" % BRIEF_DIR)
            else:
                cwd = d / "room"
                prompt = REVIEW_PROMPT.format(brief="REVIEW.md")
            argv = command(b["cli"], job["kind"], cwd, b["model"], prompt, b.get("effort", ""))
            job = load(jid)
            stem = "r%d.a%d" % (rnd, len(job.get("attempts", [])) + 1)
            job.update(cli=b["cli"], model=b["model"], account=b["account"], log=stem, argv=argv[:-1] + ["<prompt>"])
            save(job)
            t_agent = time.time()
            outside_before = main_tree_state(job) if work else None
            code, error = _spawn(jid, argv, cwd, b, stem, job.get("timeout") or DEFAULT_TIMEOUT)
            agent_sec = round(time.time() - t_agent, 1)
            job = load(jid)
            if work:
                outside = sorted(main_tree_state(job) - outside_before)
                if outside:   # the worker (or someone else meanwhile) changed the operator's tree
                    job.setdefault("warnings", []).append(
                        "while %s ran, the main working tree changed outside the worktree: %s" % (B.label(b), ", ".join(outside[:10])))
                    save(job)
            if job.get("status") == "cancelled":
                return 0
            text = read(d / ("stdout.%s.log" % stem)) + "\n" + read(d / ("stderr.%s.log" % stem))
            if work:
                made = bool(git(cwd, "status", "--porcelain", check=False)) or (cwd / BRIEF_DIR / "REPORT.md").exists()
            else:
                made = "VERDICT" in text
            outcome = ("stalled" if error.startswith("stalled") else "failed") if error else B.classify(code, text, made)
            attempt = {"round": rnd, "brain": B.label(b), "exit_code": code, "outcome": outcome, "log": stem,
                       "at": now(), "agent_sec": agent_sec}
            if outcome in ("auth", "unavailable", "blocked", "stalled"):
                long_rest = outcome in ("auth", "blocked")
                until = B.rest(b, outcome, error if outcome == "stalled" else text[-3000:],
                               cfg["auth_rest_sec"] if long_rest else B.UNKNOWN_REST if outcome == "stalled" else None)
                attempt["rested_until"] = datetime.fromtimestamp(until).isoformat(timespec="minutes")
                if outcome == "auth":
                    attempt["login"] = B.login_guide(b["cli"], b["account"])
                    opened = B.prompt_login(b["cli"], b["account"], base_env())
                    if opened and opened.get("opened_window"):
                        attempt["login_window"] = "opened %s's sign-in window on this machine" % b["cli"]
                if outcome == "blocked":
                    attempt["permission"] = ("%s stopped at a tool its headless permission mode may not use. Allow "
                                             "what it needs (see README: worker permissions) or leave it to the "
                                             "next brain." % b["cli"])
                job.setdefault("attempts", []).append(attempt)
                save(job)
                if outcome == "blocked":
                    RT.record(RT.key(b), job.get("difficulty", ""), "blocked", jid)
                if outcome == "stalled" and work:
                    RT.record(RT.key(b), job.get("difficulty", ""), "failed", jid, agent_sec=agent_sec, why="stalled")
                handover = "%s %s" % (B.label(b), {"auth": "was not signed in", "unavailable": "ran out of quota",
                                                   "blocked": "was blocked by its permission mode",
                                                   "stalled": "got stuck (no progress) and was stopped"}[outcome])
                continue
            job.setdefault("attempts", []).append(attempt)
            if outcome == "failed" and work:
                RT.record(RT.key(b), job.get("difficulty", ""), "failed", jid, agent_sec=agent_sec)
            if outcome == "failed" and work and pos + 1 < len(job["chain"]):   # escalate instead of giving up
                save(job)
                handover = "%s failed (exit %s%s)" % (B.label(b), code, ", " + error if error else "")
                continue
            job.update(exit_code=code, error=error, agent_finished=now(), agent_sec=agent_sec, brain_pos=pos,
                       brain_label=B.label(b), brain_key=RT.key(b))
            if work:
                t_commit = time.time()
                if cfg.get("commit_rounds"):   # opt-in: a commit (and the repo's hooks) per round
                    job["commit"] = commit_work(job)
                else:                          # default: stage only; the repo's hooks run once, when it lands
                    git(cwd, "add", "-A", check=False)
                job["commit_sec"] = round(time.time() - t_commit, 1)
            job["status"] = "done" if outcome == "ok" else "failed"
            if outcome == "ok" and work and job.get("check"):
                job["phase"] = "checking"   # set in the same write as "done": wait/merge must not see a gap
            break
        else:
            job = load(jid)
            signed_out = [b for b in job["chain"][job.get("chain_pos", 0):] if (B.resting(b) or {}).get("kind") == "auth"]
            logins = sorted({a["login"] for a in job.get("attempts", []) if a.get("login")} |
                            {B.login_guide(b["cli"], b["account"]) for b in signed_out})
            wait = int(cfg.get("login_wait_sec") or 0)
            if signed_out and wait > 0:   # everything else is out: open the sign-in and carry on once it lands
                env = base_env()
                for b in signed_out:
                    B.prompt_login(b["cli"], b["account"], env)
                job.update(phase="waiting_login", needs_login=logins)
                save(job)
                back = B.wait_for_login(signed_out, env, wait, stop=lambda: load(jid).get("status") == "cancelled")
                job = load(jid)
                job.pop("phase", None)
                if job.get("status") == "cancelled":
                    return 0
                if back:
                    job.pop("needs_login", None)
                    job.setdefault("warnings", []).append("waited for %s to be signed in, then carried on" % B.label(back))
                    save(job)
                    return _round(jid)
            job.update(status="failed", error="every brain in the chain is out of quota or not signed in: "
                       + "; ".join("%s until %s" % (r["group"], r["until"]) for r in B.limits()))
            if logins:
                job["needs_login"] = logins
    except Exception as e:  # noqa: BLE001 - record any failure for the director
        job = {**load(jid), "status": "failed", "error": "%s: %s" % (type(e).__name__, e)}
    job.update(finished=now(), elapsed_sec=round(time.time() - t0, 1))
    save(job)
    return 0


def commit_work(job: Dict) -> str:
    """Commit the round's changes on the job branch. A refusing commit hook leaves them staged and is reported."""
    wt = Path(job["worktree"])
    if not git(wt, "status", "--porcelain", check=False):
        return ""
    git(wt, "add", "-A")
    name, email = PROVIDERS[job["cli"]]["author"]
    msg = "shuttle(%s): %s\n\nRound %d of shuttle job %s." % (job["cli"], job["title"], job.get("round", 1), job["id"])
    p = run(["git", "-c", "user.name=" + name, "-c", "user.email=" + email, "commit", "-q", "-m", msg], cwd=wt,
            timeout=900)
    if p.returncode != 0:
        job["commit_error"] = tail((p.stdout + p.stderr).strip(), 3000)
        return ""
    job.pop("commit_error", None)
    return git(wt, "rev-parse", "HEAD")


def status(jid: str) -> Dict:
    return summary(refresh(load(jid)))


def list_jobs(active_only: bool = False, limit: int = 30) -> List[Dict]:
    if not JOBS.exists():
        return []
    out = []
    for p in sorted(JOBS.iterdir(), reverse=True):
        try:
            j = refresh(load(p.name))
        except (KeyError, ValueError):
            continue
        if active_only and j.get("status") in ("merged", "discarded"):
            continue
        out.append(summary(j))
        if len(out) >= limit:
            break
    return out


def wait(ids: List[str], timeout: int = 600, any_: bool = False, poll: float = 5.0) -> Dict:
    """Block until all (or any) of the jobs leave `running`, or until `timeout` seconds pass."""
    end = time.time() + timeout
    while True:
        states = [status(i) for i in ids]
        done = [s for s in states if s["status"] not in ("running", "starting") and not s.get("phase")]
        if (any_ and done) or len(done) == len(states) or time.time() >= end:
            keep = ("id", "status", "cli", "model", "round", "agent_sec", "check", "escalated", "warnings", "error",
                    "commit_error", "needs_login", "fell_back_from")
            return {"finished": len(done) == len(states),
                    "jobs": [{k: s_[k] for k in keep if k in s_} for s_ in states]}
        time.sleep(poll)


def result(jid: str, max_diff: int = RESULT_DIFF, paths: Optional[List[str]] = None) -> Dict:
    """What the director needs to judge a job, lean by default: report, diffstat, commits, short log tails. The
    diff itself only when asked: `paths` (only those files) and/or `max_diff` > 0 (chars). Read the worktree for
    more context instead of pulling the whole diff."""
    job = refresh(load(jid))
    d, stem = job_dir(jid), job.get("log", "")
    res = summary(job)
    out = read(d / ("stdout.%s.log" % stem))
    if job["kind"] == "review":
        res["answer"] = tail(out, 8000)
        return res
    wt = Path(job["worktree"])
    report = read(wt / BRIEF_DIR / "REPORT.md") if wt.exists() else ""
    res["stdout_tail"] = tail(out, 600 if report else TAIL)
    if job.get("status") in ("failed", "lost"):
        err = read(d / ("stderr.%s.log" % stem)).strip()
        if err:
            res["stderr_tail"] = tail(err, 1500)
    if not wt.exists():
        return res
    res["report"] = report or "(the worker wrote no REPORT.md)"
    if job.get("check_result"):
        c = job["check_result"]
        res["check_result"] = {"command": c["command"], "passed": c["passed"], "exit_code": c["exit_code"],
                               "sec": c["sec"], "tail": tail(c["tail"], 300 if c["passed"] else 1500)}
    rng = "%s..%s" % (job["base"], job["branch"])
    res["commits"] = git(wt, "log", "--format=%h %an: %s", rng, check=False)
    if job.get("commit_error"):
        res["commit_error"] = job["commit_error"]
    git(wt, "add", "-N", ".", check=False)   # so new files show in the diff below
    res["diffstat"] = git(wt, "diff", "--stat=120", job["base"], check=False)   # base vs the worktree
    if paths or max_diff > 0:
        diff = git(wt, "diff", job["base"], "--", *(paths or []), check=False)
        limit = max_diff if max_diff > 0 else 20000
        res["diff"] = diff if len(diff) <= limit else diff[:limit] + "\n...(diff truncated at %d chars; read files in %s)" % (limit, wt)
    else:
        res["diff"] = "(not included: pass paths=[...] for specific files or max_diff>0; or read files in the worktree)"
    return res


def check(jid: str, command: str, timeout: int = 1800) -> Dict:
    """Run a command in a finished job's worktree (tests, lint, build) and return only exit code and a short tail."""
    job = refresh(load(jid))
    if job["kind"] != "work" or job.get("status") in ("running", "starting") or not Path(job["worktree"]).exists():
        raise RuntimeError("job %s is %s; check needs a finished work job with its worktree" % (jid, job.get("status")))
    c = run_check(job, command, timeout)
    c["tail"] = tail(c["tail"], 400 if c["passed"] else 2000)
    return c


def followup(jid: str, feedback: str, cli: str = "", model: str = "") -> Dict:
    """Send the worker back to the same worktree with review feedback (a new round, full brief + all feedback)."""
    job = refresh(load(jid))
    if job["kind"] != "work" or job["status"] in ("running", "starting", "merged", "discarded"):
        raise RuntimeError("job %s is %s; follow-up needs a finished work job" % (jid, job["status"]))
    if not feedback.strip():
        raise ValueError("feedback is empty")
    job["feedback"] = job.get("feedback", []) + [feedback.strip()]
    job["round"] = job.get("round", 1) + 1
    job["auto_used"] = 0   # the director's round gets a fresh automatic-fix budget
    if job.get("brain_key"):
        RT.record(job["brain_key"], job.get("difficulty", ""), "rework", jid)
    if cli or model:   # a new chain led by this cli/model
        job["chain"] = make_chain(cli or job["chain"][0]["cli"], model)
        job["chain_pos"] = 0
    else:              # the brain that did the work continues; stronger ones remain as fallback
        job["chain_pos"] = job.get("brain_pos", job.get("chain_pos", 0))
    save(job)
    return launch(job)


def review(jid: str, cli: str, model: str = "", focus: str = "", timeout: int = 1200,
           chain: Optional[List[str]] = None) -> Dict:
    """A second opinion from another CLI: it gets the task, report and diff, never the worktree. Falls back along
    the reviewer's chain like a worker does."""
    brains = make_chain(cli, model, chain)
    cli, model = brains[0]["cli"], brains[0]["model"]
    parent = refresh(load(jid))
    res = result(jid, max_diff=150000)   # the reviewer's tokens, not the director's
    rid = new_id(cli) + "-review"
    job = {"id": rid, "kind": "review", "cli": cli, "model": model, "title": "review of " + parent["title"],
           "status": "starting", "round": 1, "parent": jid, "repo": parent["repo"], "timeout": int(timeout),
           "created": now(), "group": parent.get("group", ""), "chain": brains, "attempts": []}
    room = job_dir(rid) / "room"
    room.mkdir(parents=True, exist_ok=True)
    (room / "REVIEW.md").write_text(review_brief(parent, res["diff"], res.get("report", ""), focus),
                                    encoding="utf-8")
    save(job)
    return launch(job)


def cancel(jid: str) -> Dict:
    job = load(jid)
    job.pop("phase", None)
    job.update(status="cancelled", finished=now())
    save(job)
    for pid in (job.get("agent_pid"), job.get("runner_pid")):
        if pid_alive(pid):
            kill_tree(pid)
    return summary(job)


def _remove_worktree(job: Dict, delete_branch: bool) -> None:
    repo = Path(job["repo"])
    if Path(job["worktree"]).exists():
        git(repo, "worktree", "remove", "--force", job["worktree"], check=False)
    git(repo, "worktree", "prune", check=False)
    if delete_branch:
        git(repo, "branch", "-D", job["branch"], check=False)


def merge(jid: str, mode: str = "squash", message: str = "", keep: bool = False) -> Dict:
    """Land a finished job on the repo's checked-out branch, with the repo's commit hooks run once.

    squash (default): the worktree's whole change (base -> worktree) is applied to the main tree and committed
    there; nothing is committed in the worktree. ff / merge: the worktree's change is committed on the job branch
    (hooks run there), then fast-forwarded / merged without a second commit-hook run. Refuses when the main tree
    has uncommitted tracked changes."""
    job = refresh(load(jid))
    if job["kind"] != "work" or job["status"] != "done" or job.get("phase"):
        raise RuntimeError("job %s is %s%s; only a finished work job can land"
                           % (jid, job["status"], " (%s)" % job["phase"] if job.get("phase") else ""))
    repo, wt = Path(job["repo"]), Path(job["worktree"])
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("the main working tree of %s has uncommitted changes; commit or stash them first" % repo)
    git(wt, "add", "-A")
    if not git(wt, "diff", "--cached", "--stat", job["base"], check=False):
        raise RuntimeError("job %s changed nothing; nothing to land" % jid)
    target = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    msg = message or "%s\n\nShuttled to %s (job %s)." % (job["title"], job["cli"], jid)
    t0 = time.time()
    if mode == "squash":
        patch = job_dir(jid) / "land.patch"
        patch.write_bytes(subprocess.run(["git", "diff", "--cached", "--binary", job["base"]], cwd=str(wt),
                                         capture_output=True, check=True).stdout)
        p = run(["git", "apply", "--index", str(patch)], cwd=repo)   # atomic: all or nothing
        if p.returncode != 0:
            raise RuntimeError("the change does not apply to %s as it is now (it moved since the job started). "
                               "Send a followup to rebase, or land with mode=merge.\n%s"
                               % (target, (p.stderr or p.stdout).strip()[-2000:]))
        p = run(["git", "commit", "-q", "-m", msg], cwd=repo, timeout=1800)
        if p.returncode != 0:   # e.g. a commit hook refused: leave it staged in the main tree for the director
            job["commit_error"] = tail((p.stdout + p.stderr).strip(), 3000)
            save(job)
            raise RuntimeError("change staged in the main tree but its commit failed (fix, then commit by hand):\n"
                               + (p.stdout + p.stderr).strip()[-2000:])
    elif mode in ("ff", "merge"):
        if git(wt, "status", "--porcelain", check=False):
            commit_work(job)
            if job.get("commit_error"):
                save(job)
                raise RuntimeError("committing the job branch failed (its commit hook refused):\n"
                                   + job["commit_error"][-2000:])
        if mode == "ff":
            p = run(["git", "merge", "--ff-only", job["branch"]], cwd=repo)
            if p.returncode != 0:
                raise RuntimeError("fast-forward refused (%s moved since the job started); use mode=squash or merge.\n%s"
                                   % (target, (p.stderr or p.stdout).strip()))
        else:
            p = run(["git", "merge", "--no-ff", "-m", msg, job["branch"]], cwd=repo, timeout=1800)
            if p.returncode != 0:
                run(["git", "merge", "--abort"], cwd=repo)
                raise RuntimeError("merge failed; aborted.\n" + (p.stdout + p.stderr).strip()[-2000:])
    else:
        raise ValueError("mode must be squash, ff or merge")
    job.pop("commit_error", None)
    if job.get("brain_key"):
        RT.record(job["brain_key"], job.get("difficulty", ""), "accepted", jid)
    job.update(status="merged", merged=now(), merged_into=target, merged_as=git(repo, "rev-parse", "HEAD"),
               land_sec=round(time.time() - t0, 1))
    save(job)
    if not keep:
        _remove_worktree(job, delete_branch=True)
    return {**summary(job), "merged_as": job["merged_as"], "merged_into": target, "land_sec": job["land_sec"]}


def discard(jid: str) -> Dict:
    job = load(jid)
    if job.get("kind") == "work" and job.get("status") == "done" and job.get("brain_key"):
        RT.record(job["brain_key"], job.get("difficulty", ""), "rejected", jid)
    if job.get("status") in ("running", "starting"):
        cancel(jid)
        job = load(jid)
    if job["kind"] == "work":
        _remove_worktree(job, delete_branch=True)
    job.update(status="discarded", finished=job.get("finished") or now())
    return summary(save(job))


# ------------------------------------------------------------------ accounts, sign-in, chains

def auth(cli: str = "", open_login: bool = False) -> List[Dict]:
    """Sign-in state of every account of every installed CLI (or one CLI), with the login command where missing.
    `open_login`: also open the sign-in window of each one missing (claude only when named: it never runs unasked)."""
    names = [cli] if cli else [c for c in PROVIDERS if installed(c) and c not in B.disabled()]
    env = base_env()
    out = []
    for c in names:
        if c not in B.ACCOUNT_ENV:
            out.append({"cli": c, "account": "default", "logged_in": None, "detail": "custom CLI: not checked"})
            continue
        for a in B.accounts(c):
            st = B.auth_status(c, a, env)
            if open_login and not st["logged_in"] and (c != "claude" or cli == "claude"):
                opened = B.prompt_login(c, a, env)
                if opened and opened.get("opened_window"):
                    st["login_window"] = "opened; finish the sign-in there"
            out.append(st)
    return out


def login(cli: str, account: str = "default") -> Dict:
    """Open the CLI's own sign-in for `account` (a new one is created) in a new console; the operator finishes it."""
    if cli not in B.LOGIN_ARGV:
        raise ValueError("no login flow known for %r" % cli)
    res = B.open_login(cli, account, base_env())
    B.clear_limits("%s@%s#" % (cli, account))   # a fresh sign-in ends a rest that was for a missing login
    return res


def dashboard(port: int = 8765, open_browser: bool = True) -> Dict:
    """Start the local management dashboard (dashboard.py) in the background unless it is already up."""
    import socket
    import webbrowser
    url = "http://127.0.0.1:%d/" % port
    with socket.socket() as s:
        s.settimeout(0.5)
        up = s.connect_ex(("127.0.0.1", port)) == 0
    if not up:
        flags = 0x00000200 | 0x08000000 if IS_WIN else 0   # own group, no window
        subprocess.Popen([sys.executable, str(Path(__file__).with_name("dashboard.py")), "--port", str(port)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=not IS_WIN)
        for _ in range(40):
            time.sleep(0.25)
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
    if open_browser:
        webbrowser.open(url)
    return {"url": url, "already_running": up}


def chain_view(cli: str = "", model: str = "", chain: Optional[List[str]] = None, fallback: bool = True,
               difficulty: str = "") -> List[Dict]:
    """The brains a job started this way would try, in order, with any rest still on each."""
    out = []
    for b in make_chain(cli, model, chain, fallback, difficulty):
        r = B.resting(b)
        out.append({"brain": B.label(b), "quota_group": B.quota_group(b),
                    "resting_until": datetime.fromtimestamp(r["until"]).isoformat(timespec="minutes") if r else "",
                    "rest_kind": (r or {}).get("kind", "")})
    return out


def _enable_grok_plugin(cfg: Path) -> None:
    import re
    text = read(cfg)
    if not text:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('[plugins]\nenabled = ["shuttle"]\n', encoding="utf-8")
        return
    m = re.search(r'(?m)^(\[plugins\]\s*\r?\n(?:[^\[]*?\r?\n)*?enabled\s*=\s*\[)([^\]]*)(\])', text)
    if m:
        items = [x.strip().strip('"\'') for x in m.group(2).split(",") if x.strip() and x.strip().strip('"\'') != "delegate"]
        if "shuttle" not in items:
            items.append("shuttle")
        body = ", ".join('"%s"' % x for x in items)
        text = text[:m.start()] + m.group(1) + body + m.group(3) + text[m.end():]
        cfg.write_text(text, encoding="utf-8")
    elif "[plugins]" not in text:
        cfg.write_text(text.rstrip() + '\n\n[plugins]\nenabled = ["shuttle"]\n', encoding="utf-8")


def install(agent: str = "all") -> List[Dict]:
    """Install or update the shuttle plugin across installed host agents (claude, codex, agy, grok, opencode)."""
    repo_root = Path(os.path.abspath(__file__)).parents[1]
    alias = repo_root.parent / "shuttle"
    if alias.exists() and os.path.samefile(repo_root, alias):
        repo_root = alias
    plugin_dir = repo_root
    targets = ["claude", "codex", "agy", "grok", "opencode"] if agent in ("", "all") else [agent]
    out: List[Dict] = []
    for name in targets:
        if name == "claude":
            if not shutil.which("claude"):
                out.append({"agent": name, "installed": False, "detail": "claude CLI not on PATH"})
                continue
            run(["claude", "plugin", "uninstall", "delegate@claude-delegate"])
            run(["claude", "plugin", "uninstall", "shuttle@shuttle"])
            run(["claude", "plugin", "marketplace", "remove", "claude-delegate"])
            run(["claude", "plugin", "marketplace", "add", str(repo_root)])
            p = run(["claude", "plugin", "install", "shuttle@shuttle"])
            out.append({"agent": name, "installed": p.returncode == 0,
                        "detail": (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr).strip() else "ok"})
        elif name == "codex":
            if not shutil.which("codex"):
                out.append({"agent": name, "installed": False, "detail": "codex CLI not on PATH"})
                continue
            run(["codex", "plugin", "remove", "delegate@claude-delegate"])
            run(["codex", "plugin", "marketplace", "remove", "claude-delegate"])
            run(["codex", "plugin", "marketplace", "add", str(repo_root)])
            p = run(["codex", "plugin", "add", "shuttle@shuttle"])
            out.append({"agent": name, "installed": p.returncode == 0,
                        "detail": (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr).strip() else "ok"})
        elif name == "agy":
            exe = shutil.which("agy-real") or shutil.which("agy")
            if not exe:
                out.append({"agent": name, "installed": False, "detail": "agy CLI not on PATH"})
                continue
            run([exe, "plugin", "uninstall", "delegate"])
            p = run([exe, "plugin", "install", str(plugin_dir), "--force"])
            out.append({"agent": name, "installed": p.returncode == 0,
                        "detail": (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr).strip() else "ok"})
        elif name == "grok":
            if not shutil.which("grok"):
                out.append({"agent": name, "installed": False, "detail": "grok CLI not on PATH"})
                continue
            run(["grok", "plugin", "remove", "delegate"])
            run(["grok", "plugin", "remove", "shuttle"])
            p = run(["grok", "plugin", "install", str(plugin_dir), "--trust"])
            _enable_grok_plugin(Path.home() / ".grok" / "config.toml")
            out.append({"agent": name, "installed": p.returncode == 0,
                        "detail": (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr).strip() else "ok"})
        elif name == "opencode":
            cfg_dir = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "opencode"
            cfg_path = cfg_dir / "opencode.json"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            except ValueError:
                cfg = {}
            servers = cfg.setdefault("mcp", {}).setdefault("servers", {})
            servers.pop("delegate", None)
            servers["shuttle"] = {"type": "local",
                                  "command": ["python", (plugin_dir / "scripts" / "mcp_server.py").as_posix()]}
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            skills_dst = cfg_dir / "skills"
            for old in ("delegate", "delegations", "delegate-auth"):
                shutil.rmtree(skills_dst / old, ignore_errors=True)
            for sdir in (plugin_dir / "skills").iterdir():
                if sdir.is_dir() and (sdir / "SKILL.md").exists():
                    shutil.copytree(sdir, skills_dst / sdir.name, dirs_exist_ok=True)
            out.append({"agent": name, "installed": True, "detail": str(cfg_path)})
        else:
            raise ValueError("unknown host agent %r (expected claude, codex, agy, grok, opencode, or all)" % name)
    return out


# ------------------------------------------------------------------ command line

def _text(args, name: str) -> str:
    f = getattr(args, name + "_file", None)
    if f:
        return sys.stdin.read() if f == "-" else Path(f).read_text(encoding="utf-8")
    return getattr(args, name, None) or ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="shuttle", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("clis")
    s = sub.add_parser("install", help="install/update the shuttle plugin across host agents (claude, codex, agy, grok, opencode)")
    s.add_argument("agent", nargs="?", default="all", choices=["all", "claude", "codex", "agy", "grok", "opencode"])
    s = sub.add_parser("start")
    s.add_argument("--cli", default="", help="one CLI, or several comma-separated (compete mode); default agy")
    s.add_argument("--repo", default=".")
    s.add_argument("--title", default="")
    s.add_argument("--task")
    s.add_argument("--task-file")
    s.add_argument("--model", default="")
    s.add_argument("--base", default="")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    s.add_argument("--chain", default="", help="explicit brains: cli[:model][@account],... (overrides --cli order)")
    s.add_argument("--no-fallback", action="store_true", help="only --cli's own models and accounts")
    s.add_argument("--check", default="", help="acceptance command run in the worktree after each round")
    s.add_argument("--difficulty", default="", choices=["", "easy", "normal", "hard"],
                   help="pick the tier's brains (when no --cli/--chain): easy/normal start on free opencode")
    s.add_argument("--auto-fix", type=int, default=2, help="automatic rounds when --check fails")
    sub.add_parser("ratings", help="per brain and difficulty: score and history")
    sub.add_parser("agy-switch", help="sign agy in with another Google account (OAuth link + pasted code)")
    s = sub.add_parser("check")
    s.add_argument("id")
    s.add_argument("command")
    s = sub.add_parser("auth")
    s.add_argument("cli", nargs="?", default="")
    s.add_argument("--open", action="store_true", help="open the sign-in window of each one missing")
    s = sub.add_parser("login")
    s.add_argument("cli")
    s.add_argument("account", nargs="?", default="default")
    s = sub.add_parser("chain")
    s.add_argument("cli", nargs="?", default="", help="omit to see a difficulty tier")
    s.add_argument("--difficulty", default="")
    s.add_argument("--model", default="")
    s.add_argument("--no-fallback", action="store_true")
    s = sub.add_parser("limits")
    s.add_argument("--clear", nargs="?", const="*", default=None, help="clear all rests, or those matching a group")
    s = sub.add_parser("list")
    s.add_argument("--active", action="store_true")
    s.add_argument("--limit", type=int, default=30)
    sub.add_parser("status").add_argument("id")
    s = sub.add_parser("wait")
    s.add_argument("ids", nargs="+")
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--any", action="store_true")
    s = sub.add_parser("result")
    s.add_argument("id")
    s.add_argument("--max-diff", type=int, default=RESULT_DIFF)
    s.add_argument("--paths", default="", help="comma-separated files whose diff to include")
    s = sub.add_parser("followup")
    s.add_argument("id")
    s.add_argument("--feedback")
    s.add_argument("--feedback-file")
    s.add_argument("--cli", default="")
    s.add_argument("--model", default="")
    s = sub.add_parser("review")
    s.add_argument("id")
    s.add_argument("--cli", required=True)
    s.add_argument("--model", default="")
    s.add_argument("--focus", default="")
    s = sub.add_parser("merge")
    s.add_argument("id")
    s.add_argument("--mode", default="squash", choices=["squash", "ff", "merge"])
    s.add_argument("--message", default="")
    s.add_argument("--keep", action="store_true")
    sub.add_parser("cancel").add_argument("id")
    sub.add_parser("discard").add_argument("id")
    sub.add_parser("_run").add_argument("id")
    s = sub.add_parser("dashboard", help="open the local management dashboard (sign-ins, rests, jobs)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "_run":
        return _run(a.id)
    try:
        if a.cmd == "clis":
            out = clis()
        elif a.cmd == "install":
            out = install(a.agent)
        elif a.cmd == "start":
            names = [c.strip() for c in a.cli.split(",") if c.strip()] or [""]
            kw = dict(repo=a.repo, title=a.title, model=a.model, base=a.base, timeout=a.timeout, check=a.check,
                      auto_fix=a.auto_fix, difficulty=a.difficulty)
            if a.chain:
                out = start(names[0], _text(a, "task"), chain=a.chain.split(","), **kw)
            elif len(names) == 1:
                out = start(names[0], _text(a, "task"), fallback=not a.no_fallback, **kw)
            else:
                out = start_many(names, _text(a, "task"), **kw)
        elif a.cmd == "ratings":
            out = RT.table()
        elif a.cmd == "agy-switch":
            import agy_login
            return agy_login.interactive()
        elif a.cmd == "check":
            out = check(a.id, a.command)
        elif a.cmd == "auth":
            out = auth(a.cli, a.open)
        elif a.cmd == "login":
            out = login(a.cli, a.account)
        elif a.cmd == "chain":
            out = chain_view(a.cli, a.model, None, not a.no_fallback, a.difficulty)
        elif a.cmd == "limits":
            out = {"cleared": B.clear_limits("" if a.clear == "*" else a.clear)} if a.clear else B.limits()
        elif a.cmd == "list":
            out = list_jobs(a.active, a.limit)
        elif a.cmd == "status":
            out = status(a.id)
        elif a.cmd == "wait":
            out = wait(a.ids, a.timeout, a.any)
        elif a.cmd == "result":
            out = result(a.id, a.max_diff, [x for x in a.paths.split(",") if x])
        elif a.cmd == "followup":
            out = followup(a.id, _text(a, "feedback"), a.cli, a.model)
        elif a.cmd == "review":
            out = review(a.id, a.cli, a.model, a.focus)
        elif a.cmd == "merge":
            out = merge(a.id, a.mode, a.message, a.keep)
        elif a.cmd == "cancel":
            out = cancel(a.id)
        elif a.cmd == "dashboard":
            out = dashboard(a.port, not a.no_open)
        else:
            out = discard(a.id)
    except (RuntimeError, ValueError, KeyError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
