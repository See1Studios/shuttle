"""opencode runs its free built-in models without a sign-in, and a signed-out worker gets its sign-in window opened
(throttled) and waited for. No CLI is started: the probes and the window are stubbed.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("SHUTTLE_HOME", tempfile.mkdtemp(prefix="shuttle-test-"))
os.environ["SHUTTLE_NO_DASHBOARD"] = "1"
SCRIPTS = Path(os.environ.get("SHUTTLE_SCRIPTS") or Path(__file__).resolve().parents[1] / "scripts")
sys.path.insert(0, str(SCRIPTS))
import brains as B  # noqa: E402

LISTING = "opencode/big-pickle\nopencode/cat-jam-free\nopencode/glm-5.3-flash-free\nopencode/deepseek-v4.1-flash-free\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shuttle-login-"))
        self.saved = (B.CONFIG, B.ACCOUNTS, B.LIMITS, B.LOGIN_PROMPTS, B.OPENCODE_MODELS, B.HOME,
                      B._probe, B.open_login, B.auth_status, B.IS_WIN, B.AGY_MODELS)
        B.HOME, B.AGY_MODELS = self.tmp, self.tmp / "agy-models.json"
        B.CONFIG, B.ACCOUNTS, B.LIMITS = self.tmp / "brains.json", self.tmp / "accounts", self.tmp / "limits.json"
        B.LOGIN_PROMPTS, B.OPENCODE_MODELS = self.tmp / "login-prompts.json", self.tmp / "opencode-models.json"

    def tearDown(self):
        (B.CONFIG, B.ACCOUNTS, B.LIMITS, B.LOGIN_PROMPTS, B.OPENCODE_MODELS, B.HOME,
         B._probe, B.open_login, B.auth_status, B.IS_WIN, B.AGY_MODELS) = self.saved


class FreeModels(Base):
    def test_model_less_opencode_becomes_its_best_free_models(self):
        B._probe = lambda argv, env, timeout=40: (0, LISTING)
        out = B.expand([{"cli": "opencode", "model": "", "account": "default"},
                        {"cli": "agy", "model": "gemini-3.8-flash-high", "account": "default"}])
        self.assertEqual([b["model"] for b in out],
                         ["opencode/deepseek-v4.1-flash-free", "opencode/big-pickle", "opencode/glm-5.3-flash-free",
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


LIVE = ["gemini-4-argon-medium", "gemini-4-argon-high", "gemini-4-argon-low", "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium", "gemini-3.8-flash-low", "claude-opus-5-5-low", "claude-opus-5-5-medium",
        "claude-opus-5-5-high", "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-sonnet-5-5-high",
        "gpt-oss-120b-medium", "gemini-2.5-flash-high"]


class AgyLadder(Base):
    def test_families_smartest_first_newest_version_ranked(self):
        rows = B.agy_ladder(LIVE, exclude=[])
        self.assertEqual([r["family"] for r in rows if r["rank"] is not None],
                         ["gemini-4-argon", "claude-opus-5-5", "claude-sonnet-5-5", "gemini-3.8-flash"])
        older = [r for r in rows if r.get("older")]
        self.assertEqual([r["family"] for r in older], ["gemini-2.5-flash"])

    def test_exclude_models_filters_argon_and_opus(self):
        rows = B.agy_ladder(LIVE)
        self.assertEqual([r["family"] for r in rows if r["rank"] is not None],
                         ["claude-sonnet-5-5", "gemini-3.8-flash"])
        hard = B.auto_tier("hard", rows)
        self.assertNotIn("argon", " ".join(hard))
        self.assertNotIn("opus", " ".join(hard))

    def test_tiers_pick_model_and_effort_by_difficulty(self):
        lad = B.agy_ladder(LIVE, exclude=[])
        self.assertEqual(B.auto_tier("hard", lad), ["agy:gemini-4-argon-high", "agy:claude-opus-5-5-high",
                                                   "agy:claude-sonnet-5-5-high", "agy:gemini-3.8-flash-high"])
        self.assertEqual(B.auto_tier("easy", lad), ["opencode", "agy:gemini-3.8-flash-low",
                                                   "agy:gemini-3.8-flash-medium", "agy:gemini-3.8-flash-high"])
        self.assertEqual(B.auto_tier("normal", lad), ["opencode", "agy:gemini-3.8-flash-high",
                                                     "agy:claude-sonnet-5-5-medium", "agy:claude-opus-5-5-medium",
                                                     "agy:gemini-4-argon-medium"])

    def test_a_new_model_is_picked_up_without_config(self):
        lad = B.agy_ladder(LIVE + ["gemini-5-argon-high", "gemini-5-argon-low"], exclude=[])
        self.assertEqual(B.auto_tier("hard", lad)[0], "agy:gemini-5-argon-high")
        self.assertEqual(B.agy_model(lad[0], "medium"), "gemini-5-argon-high")   # nearest effort it has

    def test_argon_is_its_own_quota_group(self):
        g = lambda m: B.quota_group({"cli": "agy", "model": m, "account": "x"})   # noqa: E731
        self.assertEqual(g("gemini-4-argon-high"), "agy@x#argon")
        self.assertEqual(g("gemini-3.8-flash-low"), "agy@x#gemini")
        self.assertEqual(g("claude-opus-5-5-high"), "agy@x#claude-gpt")

    def test_brains_json_tier_still_wins(self):
        B.CONFIG.write_text(json.dumps({"tiers": {"hard": ["agy:claude-opus-5-5-high"]}, "exclude_models": []}), encoding="utf-8")
        B.AGY_MODELS.write_text(json.dumps({"at": time.time(), "models": LIVE}), encoding="utf-8")
        t = B.tiers()
        self.assertEqual(t["hard"], ["agy:claude-opus-5-5-high"])
        self.assertEqual(t["easy"][1], "agy:gemini-3.8-flash-low")


class WorkerGuards(Base):
    def test_opencode_workers_deny_outside_reads_instead_of_aborting(self):
        import shuttle as D
        cfg = json.loads(D.agent_env("opencode")["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(cfg["permission"]["external_directory"], "deny")

    def test_a_silent_worker_is_stopped_as_stalled(self):
        import shuttle as D
        B.CONFIG.write_text(json.dumps({"stall_sec": 2}), encoding="utf-8")
        jid = "test-stall"
        D.job_dir(jid).mkdir(parents=True, exist_ok=True)
        D.save({"id": jid, "status": "running"})
        room = self.tmp / "room"
        room.mkdir()
        t = time.time()
        code, error = D._spawn(jid, [sys.executable, "-c", "import time; time.sleep(60)"], room,
                               {"cli": "opencode", "model": "", "account": "default"}, "s1", 120)
        self.assertTrue(error.startswith("stalled"), error)
        self.assertLess(time.time() - t, 20)

    def test_a_worker_that_keeps_writing_is_not_stalled(self):
        import shuttle as D
        B.CONFIG.write_text(json.dumps({"stall_sec": 2}), encoding="utf-8")
        jid = "test-busy"
        D.job_dir(jid).mkdir(parents=True, exist_ok=True)
        D.save({"id": jid, "status": "running"})
        room = self.tmp / "room2"
        room.mkdir()
        busy = "import time\nfor i in range(6):\n    print(i, flush=True); time.sleep(1)"
        code, error = D._spawn(jid, [sys.executable, "-c", busy], room,
                               {"cli": "opencode", "model": "", "account": "default"}, "s1", 120)
        self.assertEqual((code, error), (0, ""))


if __name__ == "__main__":
    unittest.main()
