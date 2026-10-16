"""opencode runs its free built-in models without a sign-in, and a signed-out worker gets its sign-in window opened
(throttled) and waited for. No CLI is started: the probes and the window are stubbed.

Run: python -m unittest discover -s plugins/delegate/tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("DELEGATE_HOME", tempfile.mkdtemp(prefix="delegate-test-"))
SCRIPTS = Path(os.environ.get("DELEGATE_SCRIPTS") or Path(__file__).resolve().parents[1] / "scripts")
sys.path.insert(0, str(SCRIPTS))
import brains as B  # noqa: E402

LISTING = "opencode/big-pickle\nopencode/cat-jam-free\nopencode/glm-5.3-flash-free\nopencode/deepseek-v4.1-flash-free\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="delegate-login-"))
        self.saved = (B.CONFIG, B.ACCOUNTS, B.LIMITS, B.LOGIN_PROMPTS, B.OPENCODE_MODELS, B.HOME,
                      B._probe, B.open_login, B.auth_status, B.IS_WIN)
        B.HOME = self.tmp
        B.CONFIG, B.ACCOUNTS, B.LIMITS = self.tmp / "brains.json", self.tmp / "accounts", self.tmp / "limits.json"
        B.LOGIN_PROMPTS, B.OPENCODE_MODELS = self.tmp / "login-prompts.json", self.tmp / "opencode-models.json"

    def tearDown(self):
        (B.CONFIG, B.ACCOUNTS, B.LIMITS, B.LOGIN_PROMPTS, B.OPENCODE_MODELS, B.HOME,
         B._probe, B.open_login, B.auth_status, B.IS_WIN) = self.saved


class FreeModels(Base):
    def test_model_less_opencode_becomes_its_best_free_models(self):
        B._probe = lambda argv, env, timeout=40: (0, LISTING)
        out = B.expand([{"cli": "opencode", "model": "", "account": "default"},
                        {"cli": "agy", "model": "gemini-3.8-flash-high", "account": "default"}])
        self.assertEqual([b["model"] for b in out],
                         ["opencode/deepseek-v4.1-flash-free", "opencode/glm-5.3-flash-free", "opencode/big-pickle",
                          "gemini-3.8-flash-high"])

    def test_an_empty_listing_is_retried_then_falls_back_to_the_cache(self):
        B.OPENCODE_MODELS.write_text(json.dumps({"at": 0, "models": ["opencode/big-pickle"]}), encoding="utf-8")
        calls = []
        B._probe = lambda argv, env, timeout=40: (calls.append(1), (0, ""))[1]
        self.assertEqual(B.opencode_free_models(), ["opencode/big-pickle"])
        self.assertEqual(len(calls), 3)

    def test_each_free_model_rests_on_its_own(self):
        a = {"cli": "opencode", "model": "opencode/glm-5.3-flash-free", "account": "default"}
        b = {**a, "model": "opencode/big-pickle"}
        B.rest(a, "unavailable", "429")
        self.assertTrue(B.resting(a))
        self.assertIsNone(B.resting(b))

    def test_opencode_is_never_reported_signed_out(self):
        B._probe = lambda argv, env, timeout=40: (0, "")
        self.assertTrue(B.auth_status("opencode", "default", {})["logged_in"])


class LoginWindow(Base):
    def setUp(self):
        super().setUp()
        self.opened = []
        B.IS_WIN = True
        B.open_login = lambda cli, account, env: (self.opened.append((cli, account)) or
                                                  {"cli": cli, "account": account, "opened_window": True})

    def test_window_opens_once_per_throttle(self):
        self.assertTrue(B.prompt_login("agy", "default", {})["opened_window"])
        self.assertIsNone(B.prompt_login("agy", "default", {}))
        self.assertTrue(B.prompt_login("agy", "work", {}))
        self.assertEqual(self.opened, [("agy", "default"), ("agy", "work")])

    def test_never_for_opencode_or_when_turned_off(self):
        self.assertIsNone(B.prompt_login("opencode", "default", {}))
        B.CONFIG.write_text(json.dumps({"login_prompt_sec": 0}), encoding="utf-8")
        self.assertIsNone(B.prompt_login("agy", "default", {}))
        self.assertEqual(self.opened, [])

    def test_wait_returns_the_brain_once_signed_in_and_lifts_its_rest(self):
        agy = {"cli": "agy", "model": "gemini-3.8-flash-high", "account": "default"}
        other = {**agy, "model": "claude-sonnet-5-5-high"}
        B.rest(agy, "auth", "not signed in", 3600)
        B.rest(other, "auth", "not signed in", 3600)
        polls = []
        B.auth_status = lambda cli, account, env: {"logged_in": len(polls.append(1) or polls) >= 2}
        t = time.time()
        self.assertEqual(B.wait_for_login([agy], {}, 5, every=0.01), agy)
        self.assertLess(time.time() - t, 2)
        self.assertIsNone(B.resting(agy))
        self.assertIsNone(B.resting(other))   # the whole account was signed in, every model family

    def test_wait_gives_up(self):
        B.auth_status = lambda cli, account, env: {"logged_in": False}
        self.assertIsNone(B.wait_for_login([{"cli": "agy", "model": "", "account": "default"}], {}, 0.05, every=0.01))


if __name__ == "__main__":
    unittest.main()
