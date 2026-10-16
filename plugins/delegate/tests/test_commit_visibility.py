"""How a job's commit is reported and timed: a refused commit hook must show in status/wait (not only in
result), the repo's hooks run once per landing by default, and agent_finished / commit_sec are recorded.

Run: python -m unittest discover -s plugins/delegate/tests -v
Hermetic: DELEGATE_HOME and the git repos live in a temp dir; no worker CLI is launched.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="delegate-test-")
os.environ["DELEGATE_HOME"] = str(Path(TMP) / "home")
os.environ["DELEGATE_NO_DASHBOARD"] = "1"
SCRIPTS = Path(os.environ.get("DELEGATE_SCRIPTS") or Path(__file__).resolve().parents[1] / "scripts")
sys.path.insert(0, str(SCRIPTS))
import delegate as D  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def make_repo(name: str, refuse_commits: bool) -> Path:
    """A repo with one commit; with refuse_commits, a pre-commit hook that always fails afterwards."""
    repo = Path(TMP) / name
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "config", "user.name", "t")
    sh(repo, "config", "user.email", "t@t")
    (repo / "a.txt").write_text("one\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "init")
    if refuse_commits:
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'fast suite: test_main_guard timed out' >&2\nexit 1\n")
        hook.chmod(0o755)
    return repo


def fake_job(jid: str, repo: Path, **kw) -> dict:
    sh(repo, "branch", "delegate/" + jid)
    job = {"id": jid, "kind": "work", "cli": "codex", "title": "t", "status": "done", "round": 1,
           "repo": str(repo), "worktree": str(repo), "branch": "delegate/" + jid,
           "base": sh(repo, "rev-parse", "HEAD"), "started": D.now(), "exit_code": 0, **kw}
    D.job_dir(jid).mkdir(parents=True, exist_ok=True)
    return D.save(job)


class CommitRefusalIsVisible(unittest.TestCase):
    def test_commit_work_records_hook_refusal_and_keeps_changes_staged(self):
        repo = make_repo("refuse", refuse_commits=True)
        job = fake_job("t-refuse", repo)
        (repo / "a.txt").write_text("two\n")
        self.assertEqual(D.commit_work(job), "")
        self.assertIn("test_main_guard", job["commit_error"])
        self.assertIn("a.txt", sh(repo, "diff", "--cached", "--name-only"))

    def test_status_and_wait_surface_commit_error(self):
        repo = make_repo("surface", refuse_commits=False)
        fake_job("t-surface", repo, commit_error="pre-commit: test_main_guard timed out after 300s")
        st = D.status("t-surface")
        self.assertIn("commit_error", st)
        self.assertIn("test_main_guard", st["commit_error"])
        w = D.wait(["t-surface"], timeout=1, poll=0.1)
        self.assertIn("commit_error", w["jobs"][0])

    def test_clean_job_has_no_commit_error(self):
        repo = make_repo("clean", refuse_commits=False)
        fake_job("t-clean", repo)
        self.assertNotIn("commit_error", D.status("t-clean"))
        self.assertNotIn("commit_error", D.wait(["t-clean"], timeout=1, poll=0.1)["jobs"][0])


class Timings(unittest.TestCase):
    def test_agent_finished_and_commit_sec_reach_status_and_result(self):
        repo = make_repo("timing", refuse_commits=False)
        fake_job("t-timing", repo, agent_finished="2026-10-15T20:00:00", agent_sec=61.0, commit_sec=369.2)
        for view in (D.status("t-timing"), D.result("t-timing")):
            self.assertEqual(view["agent_finished"], "2026-10-15T20:00:00")
            self.assertEqual(view["commit_sec"], 369.2)
        saved = json.loads((D.job_dir("t-timing") / "job.json").read_text(encoding="utf-8"))
        self.assertIn("commit_sec", saved)


class MergeKeepsRefusing(unittest.TestCase):
    def test_squash_merge_refused_by_main_hook_reports_commit_error(self):
        repo = make_repo("merge-refuse", refuse_commits=True)
        wt = Path(TMP) / "merge-refuse-wt"
        sh(repo, "worktree", "add", "-q", "-b", "delegate/t-merge", str(wt))
        (wt / "a.txt").write_text("changed\n")
        job = {"id": "t-merge", "kind": "work", "cli": "codex", "title": "t", "status": "done", "round": 1,
               "repo": str(repo), "worktree": str(wt), "branch": "delegate/t-merge",
               "base": sh(repo, "rev-parse", "HEAD"), "started": D.now()}
        D.job_dir("t-merge").mkdir(parents=True, exist_ok=True)
        D.save(job)
        with self.assertRaises(RuntimeError):
            D.merge("t-merge")
        self.assertNotEqual(D.load("t-merge")["status"], "merged")
        self.assertIn("commit_error", D.status("t-merge"))


class HooksRunOncePerLanding(unittest.TestCase):
    def test_rounds_are_not_committed_by_default(self):
        self.assertFalse(D.B.config().get("commit_rounds"))

    def test_squash_merge_runs_the_commit_hook_exactly_once(self):
        repo = make_repo("merge-once", refuse_commits=False)
        count = Path(TMP) / "hook-runs"
        (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\necho run >> '%s'\n" % count.as_posix())
        wt = Path(TMP) / "merge-once-wt"
        sh(repo, "worktree", "add", "-q", "-b", "delegate/t-once", str(wt))
        (wt / "a.txt").write_text("changed\n")
        sh(wt, "add", "-A")   # what a round leaves behind by default: staged, not committed
        job = {"id": "t-once", "kind": "work", "cli": "codex", "title": "t", "status": "done", "round": 1,
               "repo": str(repo), "worktree": str(wt), "branch": "delegate/t-once",
               "base": sh(repo, "rev-parse", "HEAD"), "started": D.now()}
        D.job_dir("t-once").mkdir(parents=True, exist_ok=True)
        D.save(job)
        D.merge("t-once")
        self.assertEqual(count.read_text().count("run"), 1)
        self.assertEqual(D.load("t-once")["status"], "merged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
