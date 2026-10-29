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


if __name__ == "__main__":
    unittest.main()
