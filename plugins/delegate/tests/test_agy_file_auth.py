"""agy runs with SSH_CONNECTION set, so its login is a token file under its home (one per account) instead of the
OS credential store's single entry. Checks the environment only; no agy process is started.

Run: python -m unittest discover -s plugins/delegate/tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DELEGATE_HOME", tempfile.mkdtemp(prefix="delegate-test-"))
SCRIPTS = Path(os.environ.get("DELEGATE_SCRIPTS") or Path(__file__).resolve().parents[1] / "scripts")
sys.path.insert(0, str(SCRIPTS))
import brains as B  # noqa: E402
import delegate as D  # noqa: E402


class AgyFileAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="delegate-agy-"))
        self.saved = (B.CONFIG, B.ACCOUNTS)
        B.CONFIG, B.ACCOUNTS = self.tmp / "brains.json", self.tmp / "accounts"

    def tearDown(self):
        B.CONFIG, B.ACCOUNTS = self.saved

    def test_default_account_uses_the_file_store(self):
        self.assertEqual(B.account_env("agy", "default"), {"SSH_CONNECTION": "local"})
        self.assertEqual(D.agent_env("agy")["SSH_CONNECTION"], "local")

    def test_other_clis_are_untouched(self):
        self.assertNotIn("SSH_CONNECTION", B.account_env("codex", "default"))
        self.assertNotIn("SSH_CONNECTION", B.account_env("opencode", "default"))

    def test_named_account_gets_its_own_home_and_file_store(self):
        d = B.add_account("agy", "work")   # refused on Windows before file auth
        env = B.account_env("agy", "work")
        self.assertEqual(env["HOME"], str(d))
        self.assertEqual(env["USERPROFILE"], str(d))
        self.assertEqual(env["SSH_CONNECTION"], "local")

    def test_login_command_sets_the_variable_but_never_env_json_secrets(self):
        d = B.add_account("agy", "work2")
        (d / "env.json").write_text(json.dumps({"SECRET_KEY": "s3cret"}), encoding="utf-8")
        cmd = B.login_command("agy", "work2")
        self.assertIn("SSH_CONNECTION", cmd)
        self.assertIn(str(d), cmd)
        self.assertNotIn("s3cret", cmd)
        self.assertIn("SSH_CONNECTION", B.login_command("agy", "default"))

    def test_switch_off_restores_the_credential_store(self):
        B.CONFIG.write_text(json.dumps({"agy_file_auth": False}), encoding="utf-8")
        self.assertEqual(B.account_env("agy", "default"), {})
        if B.IS_WIN:
            with self.assertRaises(ValueError):
                B.add_account("agy", "work3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
