"""Tests for one-click auto login code extraction and multi-account rotation."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import auto_login as AL  # noqa: E402
import brains as B  # noqa: E402


class AutoLoginExtraction(unittest.TestCase):
    def test_extract_code_from_redirect_url(self):
        url = "https://antigravity.google/oauth-callback?code=4/0AeanS-fakeCode1234567890abcdef_XYZ&scope=email"
        self.assertEqual(AL.extract_code(url), "4/0AeanS-fakeCode1234567890abcdef_XYZ")

    def test_extract_code_from_encoded_query_param(self):
        url = "https://antigravity.google/oauth-callback?code=4%2F0AeanS-fakeCode1234567890abcdef_XYZ"
        self.assertEqual(AL.extract_code(url), "4/0AeanS-fakeCode1234567890abcdef_XYZ")

    def test_extract_code_from_title_or_raw_text(self):
        text = "Google Antigravity Sign-in: 4/0AeanS-fakeCode1234567890abcdef_XYZ - Success"
        self.assertEqual(AL.extract_code(text), "4/0AeanS-fakeCode1234567890abcdef_XYZ")

    def test_extract_code_returns_none_for_unrelated_text(self):
        self.assertIsNone(AL.extract_code("https://accounts.google.com/signin"))
        self.assertIsNone(AL.extract_code("error 404 not found"))


class MultiAccountFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shuttle-acct-test-"))
        self.saved = (B.CONFIG, B.ACCOUNTS, B.LIMITS, B.HOME)
        B.HOME = self.tmp
        B.CONFIG, B.ACCOUNTS, B.LIMITS = self.tmp / "brains.json", self.tmp / "accounts", self.tmp / "limits.json"

    def tearDown(self):
        (B.CONFIG, B.ACCOUNTS, B.LIMITS, B.HOME) = self.saved

    def test_add_account_registers_directory_and_lists(self):
        self.assertEqual(B.accounts("agy"), ["default"])
        B.add_account("agy", "work")
        B.add_account("agy", "acc2")
        self.assertEqual(B.accounts("agy"), ["default", "acc2", "work"])

    def test_tier_chain_expands_all_accounts(self):
        B.add_account("agy", "acc2")
        # normal tier uses opencode, fast agy (flash), then strong agy (sonnet)
        ladder = [
            {"family": "claude-sonnet-5-5", "rank": 0, "efforts": ["medium", "high"]},
            {"family": "gemini-3.8-flash", "rank": 1, "efforts": ["low", "high"]},
        ]
        chain = B.tier_chain("normal", installed=lambda c: True)
        agy_flash = [b for b in chain if b["cli"] == "agy" and "flash" in b["model"]]
        # Each agy model is spread across registered accounts: default, then acc2
        self.assertEqual([b["account"] for b in agy_flash], ["default", "acc2"])

    def test_quota_rest_on_one_account_does_not_block_other_account(self):
        B.add_account("agy", "acc2")
        b1 = {"cli": "agy", "model": "gemini-3.8-flash-high", "account": "default"}
        b2 = {"cli": "agy", "model": "gemini-3.8-flash-high", "account": "acc2"}

        self.assertIsNone(B.resting(b1))
        self.assertIsNone(B.resting(b2))

        # Rest b1 due to 429 quota exhaustion
        B.rest(b1, "unavailable", "429 rate limit exceeded", seconds=3600)

        # b1 is resting, but b2 is completely ready and available
        self.assertIsNotNone(B.resting(b1))
        self.assertIsNone(B.resting(b2))


class ExcludeExpensiveModels(unittest.TestCase):
    def test_exclude_models_defaults_to_argon_and_opus(self):
        cfg = B.config()
        self.assertIn("argon", cfg.get("exclude_models", []))
        self.assertIn("opus", cfg.get("exclude_models", []))

    def test_ladder_and_chain_strip_argon_and_opus(self):
        models = [
            "gemini-4-argon-high", "gemini-4-argon-medium",
            "claude-opus-5-5-high", "claude-opus-5-5-medium",
            "claude-sonnet-5-5-high", "claude-sonnet-5-5-medium",
            "gemini-3.8-flash-high"
        ]
        lad = B.agy_ladder(models)
        families = [r["family"] for r in lad]
        self.assertNotIn("gemini-4-argon", families)
        self.assertNotIn("claude-opus-5-5", families)
        self.assertIn("claude-sonnet-5-5", families)
        self.assertIn("gemini-3.8-flash", families)


class HeartbeatAndLiveProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shuttle-hb-test-"))
        import shuttle as D
        self.D = D
        self.saved_jobs = D.JOBS
        D.JOBS = self.tmp / "jobs"
        D.JOBS.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.D.JOBS = self.saved_jobs

    def test_load_and_summary_surface_live_heartbeat(self):
        jid = "test-live-hb"
        d = self.D.job_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        job = {
            "id": jid,
            "status": "running",
            "cli": "agy",
            "model": "gemini-3.8-flash-high",
            "title": "testing live progress",
            "round": 1,
        }
        self.D.save(job)

        # Before heartbeat file exists
        loaded = self.D.load(jid)
        self.assertNotIn("heartbeat", loaded)
        summ = self.D.summary(loaded)
        self.assertNotIn("heartbeat", summ)

        # When heartbeat is recorded by runner
        hb = {
            "at": 1700000000.0,
            "agent_pid": 12345,
            "brain": "agy:gemini-3.8-flash-high@default",
            "elapsed_sec": 12.3,
            "active_sec_ago": 2,
            "state": "active",
            "stall_sec": 240,
            "stall_in": 238,
            "last_line": "running tests: 5 passed",
            "files_changed": 3,
        }
        (d / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")

        loaded = self.D.load(jid)
        self.assertIn("heartbeat", loaded)
        self.assertEqual(loaded["heartbeat"]["state"], "active")
        self.assertEqual(loaded["heartbeat"]["files_changed"], 3)

        summ = self.D.summary(loaded)
        self.assertIn("heartbeat", summ)
        self.assertEqual(summ["heartbeat"]["stall_in"], 238)

    def test_get_latest_activity_reads_log_and_git_changes(self):
        jid = "test-act"
        d = self.D.job_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        stem = "r1.a1"
        (d / f"stdout.{stem}.log").write_text("Compiling code...\nAll checks passed successfully!\n", encoding="utf-8")
        room = self.tmp / "worktree"
        room.mkdir(parents=True, exist_ok=True)

        last_line, changed = self.D._get_latest_activity({"cli": "opencode", "account": "default"}, d, stem, room)
        self.assertEqual(last_line, "All checks passed successfully!")
        self.assertEqual(changed, 0)


if __name__ == "__main__":
    unittest.main()
