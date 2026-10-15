#!/usr/bin/env python3
"""Delegate coding tasks to other agent CLIs in isolated git worktrees, then review and land the result.

Claude (or any orchestrator) is the director: it writes a brief, hands it to a worker CLI (codex, claude, grok,
agy, opencode) running headless in its own worktree, waits, reviews the diff, asks for rework or lands it.

The same functions back the MCP server (mcp_server.py) and this command line:

    delegate.py clis
    delegate.py start --cli codex --repo . --title "..." --task-file brief.md [--model M] [--timeout 3600]
    delegate.py list | status <id> | wait <id>... [--timeout 600] | result <id> [--max-diff N]
    delegate.py followup <id> --feedback-file f.md | review <id> --cli claude | cancel <id>
    delegate.py merge <id> [--mode squash|ff|merge] [--message M] [--keep] | discard <id>

State lives under $DELEGATE_HOME (default ~/.claude-delegate): jobs/<id>/job.json, logs, and worktrees/<id>.
Worker command lines default to each CLI's sandboxed / edit-only permission mode. To change them, the operator
writes $DELEGATE_HOME/providers.json (see README); this script never widens them by itself.
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

HOME =Path(os.environ.get("DELEGATE_HOME") or Path.home() / ".claude-delegate")
JOBS = HOME / "jobs"
WORKTREES = HOME / "worktrees"
IS_WIN = os.name == "nt"
BRIEF_DIR = ".delegate"            # inside each worktree, git-excluded: TASK.md in, REPORT.md out
DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_DIFF = 60000
TAIL = 4000

# Each CLI headless, in its own restricted mode. `work`: may edit files in its cwd (the worktree). `review`:
# read-only; the reviewer also runs in a separate room holding only the review brief, never the worktree.
# `extra` flags go before a trailing "-p" so the prompt stays last. `author`: git identity of the runner's commit.
PROVIDERS: Dict[str, Dict] = {
    "codex": {"work": ["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check"],
              "review": ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"],
              "model_flag": "-m", "author": ("Codex", "codex@localhost"),
              "note": "OpenAI Codex CLI; workspace-write sandbox, can run tests"},
    "claude": {"work": ["claude", "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Glob,Grep", "-p"],
               "review": ["claude", "--allowedTools", "Read,Grep,Glob", "-p"],
               "model_flag": "--model", "author": ("Claude Code", "noreply@anthropic.com"),
               "note": "Claude Code, fresh context; edits only (no shell) unless providers.json allows more"},
    "grok": {"work": ["grok", "--permission-mode", "acceptEdits", "-p"],
             "review": ["grok", "--permission-mode", "plan", "-p"],
             "model_flag": "-m", "author": ("Grok", "grok@localhost"), "note": "xAI Grok Build; accept-edits mode"},
    "agy": {"work": ["agy-real", "--sandbox", "--mode", "accept-edits", "-p"],
            "review": ["agy-real", "--sandbox", "--mode", "plan", "-p"],
            "model_flag": "--model", "workdir_flag": "--add-dir", "author": ("agy", "agy@localhost"),
            "note": "Antigravity (Gemini); sandboxed, accept-edits mode"},
    "opencode": {"work": ["opencode", "run"], "review": ["opencode", "run", "--agent", "plan"],
                 "model_flag": "-m", "author": ("opencode", "opencode@localhost"),
                 "note": "opencode; uses its own configured permissions"},
}


def load_overrides() -> None:
    """Operator-owned $DELEGATE_HOME/providers.json: per-CLI keys replace the defaults above, new CLIs are added."""
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
        raise KeyError("no such delegation job: %s" % jid)
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
            "started", "finished", "exit_code", "elapsed_sec", "error", "needs_login", "group", "parent")
    out = {k: job[k] for k in keys if job.get(k) not in (None, "", [])}
    tried = [a for a in job.get("attempts", []) if a.get("round") == job.get("round") and a.get("outcome") in ("auth", "unavailable")]
    if tried:   # brains this round skipped past (quota / sign-in)
        out["fell_back_from"] = ["%s (%s until %s)" % (a["brain"], a["outcome"], a.get("rested_until", "?")) for a in tried]
    return out


# ------------------------------------------------------------------ command lines

def with_flags(argv: List[str], flags: List[str]) -> List[str]:
    argv = list(argv)
    at = len(argv) - 1 if argv and argv[-1] == "-p" else len(argv)
    argv[at:at] = flags
    return argv


def command(cli: str, kind: str, cwd: Path, model: str, prompt: str) -> List[str]:
    spec = PROVIDERS[cli]
    flags: List[str] = []
    if kind == "work" and spec.get("workdir_flag"):
        flags += [spec["workdir_flag"], str(cwd)]
    if model:
        flags += [spec["model_flag"], model]
    argv = with_flags(spec[kind], flags) + [prompt]
    exe = shutil.which(argv[0])
    if not exe:
        raise RuntimeError("%s CLI is not installed or not on PATH" % argv[0])
    argv[0] = exe
    return argv


def base_env() -> Dict[str, str]:
    """This process's environment minus the orchestrator's own session: a worker uses its own CLI login."""
    env = dict(os.environ)
    hosted = "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH" in env   # the desktop app routes its own session's auth
    for k in list(env):
        if k.startswith("CLAUDE") or (hosted and k == "ANTHROPIC_BASE_URL"):
            env.pop(k, None)
    return env


def agent_env(cli: str, account: str = "default") -> Dict[str, str]:
    env = base_env()
    env.update(B.account_env(cli, account))
    name, email = PROVIDERS[cli]["author"]
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
               DELEGATE_WORKER="1")
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
        rounds = ("\n\nThis is a rework round. Your earlier rounds are already committed on this branch: everything "
                  "in `git diff %s` (the task's base commit) is this task's own work, not pre-existing code, and "
                  "the reviewer may ask you to undo any of it." % job["base"][:12]) + rounds
    return """# Delegated task: {title}

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
        out.append({"cli": name, "installed": bool(exe), "path": exe or "", "note": spec.get("note", "")})
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


def make_chain(cli: str = "", model: str = "", chain: Optional[List[str]] = None, fallback: bool = True) -> List[Dict]:
    """The brains a job may use, in order: an explicit chain of 'cli[:model][@account]', else the default chain for
    `cli` (its models x accounts, then its fallback CLIs' models x accounts when `fallback`; see brains.py)."""
    if chain:
        brains = [B.parse(s) for s in chain]
    else:
        if cli not in PROVIDERS:
            raise ValueError("unknown cli %r; choose from %s" % (cli, ", ".join(PROVIDERS)))
        brains = B.chain_for(cli, model, fallback, installed)
    bad = sorted({b["cli"] for b in brains if b["cli"] not in PROVIDERS})
    if bad:
        raise ValueError("unknown cli in chain: %s" % ", ".join(bad))
    brains = [b for b in brains if installed(b["cli"])]
    if not brains:
        raise RuntimeError("none of the chain's CLIs is installed")
    return brains


def start(cli: str, task: str, repo: str = ".", title: str = "", model: str = "", base: str = "",
          timeout: int = DEFAULT_TIMEOUT, group: str = "", chain: Optional[List[str]] = None,
          fallback: bool = True) -> Dict:
    if not task.strip():
        raise ValueError("task brief is empty")
    brains = make_chain(cli, model, chain, fallback)
    first = brains[0]
    root = Path(git(Path(repo).resolve(), "rev-parse", "--show-toplevel"))
    base_sha = git(root, "rev-parse", base or "HEAD")
    jid = new_id(first["cli"])
    wt, branch = WORKTREES / jid, "delegate/%s" % jid
    WORKTREES.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", branch, str(wt), base_sha, timeout=600)
    exclude_brief_dir(root)
    job = {"id": jid, "kind": "work", "cli": first["cli"], "model": first["model"], "account": first["account"],
           "title": title or task.strip().splitlines()[0][:80], "status": "starting", "round": 1, "repo": str(root),
           "worktree": str(wt), "branch": branch, "base": base_sha,
           "base_ref": git(root, "rev-parse", "--abbrev-ref", "HEAD"), "timeout": int(timeout), "created": now(),
           "group": group, "feedback": [], "chain": brains, "attempts": []}
    d = job_dir(jid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(task, encoding="utf-8")
    save(job)
    return launch(job)


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
    save(job)
    with open(d / "runner.log", "ab") as log:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_run", job["id"]], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, close_fds=True, **kw)
    job["runner_pid"] = p.pid
    save(job)
    return summary(job)


def _spawn(jid: str, argv: List[str], cwd: Path, b: Dict, stem: str, timeout: int) -> tuple:
    """Run one brain to its end. (exit code, error text)."""
    d = job_dir(jid)
    with open(d / ("stdout.%s.log" % stem), "wb") as out, open(d / ("stderr.%s.log" % stem), "wb") as err:
        kw = {"creationflags": 0x08000000} if IS_WIN else {"start_new_session": True}
        p = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             env=agent_env(b["cli"], b["account"]), **kw)
        job = load(jid)
        job["agent_pid"] = p.pid
        save(job)
        try:
            return p.wait(timeout=timeout), ""
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            p.wait(30)
            return -1, "timed out after %ss" % timeout


def _run(jid: str) -> int:
    """The background runner: one round of one job, down the job's chain of brains. A brain that is out (quota,
    limit, outage) or not signed in is rested for its quota group and the next one takes over the same worktree."""
    job = load(jid)
    d, t0, rnd, handover = job_dir(jid), time.time(), job.get("round", 1), ""
    work = job["kind"] == "work"
    cfg = B.config()
    try:
        for b in job["chain"]:
            if B.resting(b):
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
            argv = command(b["cli"], job["kind"], cwd, b["model"], prompt)
            job = load(jid)
            stem = "r%d.a%d" % (rnd, len(job.get("attempts", [])) + 1)
            job.update(cli=b["cli"], model=b["model"], account=b["account"], log=stem, argv=argv[:-1] + ["<prompt>"])
            save(job)
            code, error = _spawn(jid, argv, cwd, b, stem, job.get("timeout") or DEFAULT_TIMEOUT)
            job = load(jid)
            if job.get("status") == "cancelled":
                return 0
            text = read(d / ("stdout.%s.log" % stem)) + "\n" + read(d / ("stderr.%s.log" % stem))
            if work:
                made = bool(git(cwd, "status", "--porcelain", check=False)) or (cwd / BRIEF_DIR / "REPORT.md").exists()
            else:
                made = "VERDICT" in text
            outcome = "failed" if error else B.classify(code, text, made)
            attempt = {"round": rnd, "brain": B.label(b), "exit_code": code, "outcome": outcome, "log": stem,
                       "at": now()}
            if outcome in ("auth", "unavailable"):
                until = B.rest(b, outcome, text[-3000:], cfg["auth_rest_sec"] if outcome == "auth" else None)
                attempt["rested_until"] = datetime.fromtimestamp(until).isoformat(timespec="minutes")
                if outcome == "auth":
                    attempt["login"] = B.login_guide(b["cli"], b["account"])
                job.setdefault("attempts", []).append(attempt)
                save(job)
                handover = "%s %s" % (B.label(b), "was not signed in" if outcome == "auth" else "ran out of quota")
                continue
            job.setdefault("attempts", []).append(attempt)
            job.update(exit_code=code, error=error)
            if work:
                job["commit"] = commit_work(job)
            job["status"] = "done" if outcome == "ok" else "failed"
            break
        else:
            job = load(jid)
            logins = sorted({a["login"] for a in job.get("attempts", []) if a.get("login")} |
                            {B.login_guide(b["cli"], b["account"]) for b in job["chain"]
                             if (B.resting(b) or {}).get("kind") == "auth"})
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
    msg = "delegate(%s): %s\n\nRound %d of delegation job %s." % (job["cli"], job["title"], job.get("round", 1), job["id"])
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
        done = [s for s in states if s["status"] not in ("running", "starting")]
        if (any_ and done) or len(done) == len(states) or time.time() >= end:
            return {"finished": len(done) == len(states), "jobs": states}
        time.sleep(poll)


def result(jid: str, max_diff: int = DEFAULT_MAX_DIFF) -> Dict:
    job = refresh(load(jid))
    d, stem = job_dir(jid), job.get("log", "")
    res = summary(job)
    res["stdout_tail"] = tail(read(d / ("stdout.%s.log" % stem)))
    err = read(d / ("stderr.%s.log" % stem)).strip()
    if err:
        res["stderr_tail"] = tail(err, 2000)
    if job["kind"] == "review":
        return res
    wt = Path(job["worktree"])
    if not wt.exists():
        return res
    res["report"] = read(wt / BRIEF_DIR / "REPORT.md") or "(the worker wrote no REPORT.md)"
    rng = "%s..%s" % (job["base"], job["branch"])
    res["commits"] = git(wt, "log", "--format=%h %an: %s", rng, check=False)
    if job.get("commit_error"):
        res["commit_error"] = job["commit_error"] + "\n(changes are staged in the worktree, not committed)"
    git(wt, "add", "-N", ".", check=False)   # so new files show in the diff below
    res["diffstat"] = git(wt, "diff", "--stat", job["base"], check=False)
    diff = git(wt, "diff", job["base"], check=False)       # base vs the worktree: committed and uncommitted
    res["diff"] = diff if len(diff) <= max_diff else diff[:max_diff] + "\n...(diff truncated at %d chars; read files in %s)" % (max_diff, wt)
    res["uncommitted"] = git(wt, "status", "--porcelain", check=False)
    return res


def followup(jid: str, feedback: str, cli: str = "", model: str = "") -> Dict:
    """Send the worker back to the same worktree with review feedback (a new round, full brief + all feedback)."""
    job = refresh(load(jid))
    if job["kind"] != "work" or job["status"] in ("running", "starting", "merged", "discarded"):
        raise RuntimeError("job %s is %s; follow-up needs a finished work job" % (jid, job["status"]))
    if not feedback.strip():
        raise ValueError("feedback is empty")
    job["feedback"] = job.get("feedback", []) + [feedback.strip()]
    job["round"] = job.get("round", 1) + 1
    if cli or model:   # a new chain led by this cli/model; otherwise the job's chain again (rested brains skipped)
        job["chain"] = make_chain(cli or job["chain"][0]["cli"], model)
    save(job)
    return launch(job)


def review(jid: str, cli: str, model: str = "", focus: str = "", timeout: int = 1200,
           chain: Optional[List[str]] = None) -> Dict:
    """A second opinion from another CLI: it gets the task, report and diff, never the worktree. Falls back along
    the reviewer's chain like a worker does."""
    brains = make_chain(cli, model, chain)
    cli, model = brains[0]["cli"], brains[0]["model"]
    parent = refresh(load(jid))
    res = result(jid, max_diff=150000)
    rid = new_id(cli) + "-review"
    job = {"id": rid, "kind": "review", "cli": cli, "model": model, "title": "review of " + parent["title"],
           "status": "starting", "round": 1, "parent": jid, "repo": parent["repo"], "timeout": int(timeout),
           "created": now(), "group": parent.get("group", ""), "chain": brains, "attempts": []}
    room = job_dir(rid) / "room"
    room.mkdir(parents=True, exist_ok=True)
    (room / "REVIEW.md").write_text(review_brief(parent, res.get("diff", ""), res.get("report", ""), focus),
                                    encoding="utf-8")
    save(job)
    return launch(job)


def cancel(jid: str) -> Dict:
    job = load(jid)
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
    """Land the job's branch on the repo's checked-out branch. Refuses when that tree has tracked changes."""
    job = refresh(load(jid))
    if job["kind"] != "work" or job["status"] != "done":
        raise RuntimeError("job %s is %s; only a finished work job can land" % (jid, job["status"]))
    repo = Path(job["repo"])
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("the main working tree of %s has uncommitted changes; commit or stash them first" % repo)
    if not git(repo, "log", "--oneline", "%s..%s" % (job["base"], job["branch"])):
        raise RuntimeError("job %s has no commits to land%s" % (jid, "; its commit hook refused: " + job["commit_error"] if job.get("commit_error") else ""))
    target = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    msg = message or "%s\n\nDelegated to %s (job %s)." % (job["title"], job["cli"], jid)
    if mode == "ff":
        p = run(["git", "merge", "--ff-only", job["branch"]], cwd=repo)
        if p.returncode != 0:
            raise RuntimeError("fast-forward refused (%s moved since the job started); use mode=squash or merge.\n%s"
                               % (target, (p.stderr or p.stdout).strip()))
    elif mode == "merge":
        p = run(["git", "merge", "--no-ff", "-m", msg, job["branch"]], cwd=repo)
        if p.returncode != 0:
            run(["git", "merge", "--abort"], cwd=repo)
            raise RuntimeError("merge failed; aborted.\n" + (p.stdout + p.stderr).strip()[-2000:])
    elif mode == "squash":
        p = run(["git", "merge", "--squash", job["branch"]], cwd=repo)
        if p.returncode != 0:
            run(["git", "reset", "--merge"], cwd=repo)
            raise RuntimeError("squash failed; reset.\n" + (p.stdout + p.stderr).strip()[-2000:])
        p = run(["git", "commit", "-q", "-m", msg], cwd=repo)
        if p.returncode != 0:   # e.g. a commit hook refused: leave the squash staged for the director to fix
            raise RuntimeError("squash staged but commit failed (fix and commit by hand):\n" + (p.stdout + p.stderr).strip()[-2000:])
    else:
        raise ValueError("mode must be squash, ff or merge")
    job.update(status="merged", merged=now(), merged_into=target, merged_as=git(repo, "rev-parse", "HEAD"))
    save(job)
    if not keep:
        _remove_worktree(job, delete_branch=True)
    return {**summary(job), "merged_as": job["merged_as"], "merged_into": target}


def discard(jid: str) -> Dict:
    job = load(jid)
    if job.get("status") in ("running", "starting"):
        cancel(jid)
        job = load(jid)
    if job["kind"] == "work":
        _remove_worktree(job, delete_branch=True)
    job.update(status="discarded", finished=job.get("finished") or now())
    return summary(save(job))


# ------------------------------------------------------------------ accounts, sign-in, chains

def auth(cli: str = "") -> List[Dict]:
    """Sign-in state of every account of every installed CLI (or one CLI), with the login command where missing."""
    names = [cli] if cli else [c for c in PROVIDERS if installed(c)]
    env = base_env()
    out = []
    for c in names:
        if c not in B.ACCOUNT_ENV:
            out.append({"cli": c, "account": "default", "logged_in": None, "detail": "custom CLI: not checked"})
            continue
        for a in B.accounts(c):
            out.append(B.auth_status(c, a, env))
    return out


def login(cli: str, account: str = "default") -> Dict:
    """Open the CLI's own sign-in for `account` (a new one is created) in a new console; the operator finishes it."""
    if cli not in B.LOGIN_ARGV:
        raise ValueError("no login flow known for %r" % cli)
    res = B.open_login(cli, account, base_env())
    B.clear_limits("%s@%s#" % (cli, account))   # a fresh sign-in ends a rest that was for a missing login
    return res


def chain_view(cli: str, model: str = "", chain: Optional[List[str]] = None, fallback: bool = True) -> List[Dict]:
    """The brains a job started this way would try, in order, with any rest still on each."""
    out = []
    for b in make_chain(cli, model, chain, fallback):
        r = B.resting(b)
        out.append({"brain": B.label(b), "quota_group": B.quota_group(b),
                    "resting_until": datetime.fromtimestamp(r["until"]).isoformat(timespec="minutes") if r else "",
                    "rest_kind": (r or {}).get("kind", "")})
    return out


# ------------------------------------------------------------------ command line

def _text(args, name: str) -> str:
    f = getattr(args, name + "_file", None)
    if f:
        return sys.stdin.read() if f == "-" else Path(f).read_text(encoding="utf-8")
    return getattr(args, name, None) or ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="delegate", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("clis")
    s = sub.add_parser("start")
    s.add_argument("--cli", required=True, help="one CLI, or several comma-separated (compete mode)")
    s.add_argument("--repo", default=".")
    s.add_argument("--title", default="")
    s.add_argument("--task")
    s.add_argument("--task-file")
    s.add_argument("--model", default="")
    s.add_argument("--base", default="")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    s.add_argument("--chain", default="", help="explicit brains: cli[:model][@account],... (overrides --cli order)")
    s.add_argument("--no-fallback", action="store_true", help="only --cli's own models and accounts")
    sub.add_parser("auth").add_argument("cli", nargs="?", default="")
    s = sub.add_parser("login")
    s.add_argument("cli")
    s.add_argument("account", nargs="?", default="default")
    s = sub.add_parser("chain")
    s.add_argument("cli")
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
    s.add_argument("--max-diff", type=int, default=DEFAULT_MAX_DIFF)
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
    a = ap.parse_args(argv)

    if a.cmd == "_run":
        return _run(a.id)
    try:
        if a.cmd == "clis":
            out = clis()
        elif a.cmd == "start":
            names = [c.strip() for c in a.cli.split(",") if c.strip()]
            kw = dict(repo=a.repo, title=a.title, model=a.model, base=a.base, timeout=a.timeout)
            if a.chain:
                out = start(names[0], _text(a, "task"), chain=a.chain.split(","), **kw)
            elif len(names) == 1:
                out = start(names[0], _text(a, "task"), fallback=not a.no_fallback, **kw)
            else:
                out = start_many(names, _text(a, "task"), **kw)
        elif a.cmd == "auth":
            out = auth(a.cli)
        elif a.cmd == "login":
            out = login(a.cli, a.account)
        elif a.cmd == "chain":
            out = chain_view(a.cli, a.model, None, not a.no_fallback)
        elif a.cmd == "limits":
            out = {"cleared": B.clear_limits("" if a.clear == "*" else a.clear)} if a.clear else B.limits()
        elif a.cmd == "list":
            out = list_jobs(a.active, a.limit)
        elif a.cmd == "status":
            out = status(a.id)
        elif a.cmd == "wait":
            out = wait(a.ids, a.timeout, a.any)
        elif a.cmd == "result":
            out = result(a.id, a.max_diff)
        elif a.cmd == "followup":
            out = followup(a.id, _text(a, "feedback"), a.cli, a.model)
        elif a.cmd == "review":
            out = review(a.id, a.cli, a.model, a.focus)
        elif a.cmd == "merge":
            out = merge(a.id, a.mode, a.message, a.keep)
        elif a.cmd == "cancel":
            out = cancel(a.id)
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
