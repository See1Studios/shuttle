"""Multi-agent director & plugin compatibility tests for Shuttle (Claude Code, Codex, Antigravity, Grok, OpenCode).

Run: python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SHUTTLE_HOME", tempfile.mkdtemp(prefix="shuttle-test-"))
os.environ["SHUTTLE_NO_DASHBOARD"] = "1"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR
SCRIPTS = Path(os.environ.get("SHUTTLE_SCRIPTS") or PLUGIN_DIR / "scripts")
sys.path.insert(0, str(SCRIPTS))
import shuttle as D  # noqa: E402


class DirectorEnvIsolation(unittest.TestCase):
    def test_base_env_strips_all_director_and_plugin_variables(self):
        injected = {
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PLUGIN_ROOT": "/tmp/claude-plugin",
            "CODEX_SANDBOX": "workspace-write",
            "CODEX_HOME": "/tmp/director-codex",
            "ANTIGRAVITY_CLI_ALIAS": "agy",
            "ANTIGRAVITY_LS_ADDRESS": "localhost:50000",
            "GROK_HOME": "/tmp/director-grok",
            "GROK_PLUGIN_ROOT": "/tmp/grok-plugin",
            "OPENCODE_CONFIG_CONTENT": '{"director": true}',
            "PLUGIN_ROOT": "/tmp/agy-plugin",
            "PLUGIN_DATA": "/tmp/agy-data",
            "KEEP_USER_VAR": "kept",
        }
        saved = {k: os.environ.get(k) for k in injected}
        try:
            os.environ.update(injected)
            env = D.base_env()
            for k in injected:
                if k == "KEEP_USER_VAR":
                    self.assertEqual(env.get(k), "kept")
                else:
                    self.assertNotIn(k, env, "%s leaked into base_env()" % k)
            oc_env = D.agent_env("opencode")
            self.assertEqual(oc_env["SHUTTLE_WORKER"], "1")
            self.assertIn("external_directory", oc_env["OPENCODE_CONFIG_CONTENT"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_mcp_server_hides_tools_from_workers(self):
        code = (
            "import mcp_server as M; "
            "tools = getattr(M.mcp, '_tool_manager', None); "
            "print(len(tools.list_tools()) if tools else -1)"
        )
        env_dir = dict(os.environ)
        env_dir.pop("SHUTTLE_WORKER", None)
        p_dir = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), env=env_dir,
                               capture_output=True, text=True, check=True)
        self.assertGreater(int(p_dir.stdout.strip()), 10)

        env_wrk = dict(os.environ, SHUTTLE_WORKER="1")
        p_wrk = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), env=env_wrk,
                               capture_output=True, text=True, check=True)
        self.assertEqual(int(p_wrk.stdout.strip()), 0)


class MultiAgentManifestsAndSkills(unittest.TestCase):
    def test_manifests_are_valid_and_versions_match(self):
        root_manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        claude_manifest = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        codex_manifest = json.loads((PLUGIN_DIR / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(root_manifest["name"], "shuttle")
        self.assertEqual(claude_manifest["name"], "shuttle")
        self.assertEqual(codex_manifest["name"], "shuttle")
        self.assertEqual(root_manifest["version"], claude_manifest["version"])
        self.assertEqual(root_manifest["version"], codex_manifest["version"])
        self.assertNotIn("$schema", root_manifest)  # Antigravity requires legacy schema-less manifest for mcp_config.json
        self.assertTrue((PLUGIN_DIR / root_manifest["logo"]).exists())

    def test_mcp_configs_match_each_host_convention(self):
        claude_mcp = json.loads((PLUGIN_DIR / ".mcp.json").read_text(encoding="utf-8"))
        agy_mcp = json.loads((PLUGIN_DIR / "mcp_config.json").read_text(encoding="utf-8"))
        codex_mcp = json.loads((PLUGIN_DIR / ".codex-plugin" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(claude_mcp["mcpServers"]["shuttle"]["args"], ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"])
        self.assertEqual(agy_mcp["mcpServers"]["shuttle"]["args"], ["${PLUGIN_ROOT}/scripts/mcp_server.py"])
        self.assertEqual(codex_mcp["mcpServers"]["shuttle"]["args"], ["scripts/mcp_server.py"])
        self.assertEqual(codex_mcp["mcpServers"]["shuttle"]["cwd"], ".")

    def test_marketplace_and_workspace_discovery_files_exist(self):
        for rel in (".claude-plugin/marketplace.json", ".grok-plugin/marketplace.json",
                    ".agents/plugins/marketplace.json", ".agents/plugins.json"):
            p = REPO_ROOT / rel
            self.assertTrue(p.exists(), "missing %s" % rel)
            json.loads(p.read_text(encoding="utf-8"))

    def test_unified_skills_have_valid_frontmatter_and_no_legacy_commands_dir(self):
        self.assertFalse((PLUGIN_DIR / "commands").exists(), "legacy commands/ dir breaks Antigravity skill conversion")
        for name in ("orchestrate", "shuttle", "shuttles", "shuttle-auth"):
            skill_md = PLUGIN_DIR / "skills" / name / "SKILL.md"
            self.assertTrue(skill_md.exists(), "missing skill %s" % name)
            text = skill_md.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), "%s missing YAML frontmatter" % name)
            front = text.split("---\n", 2)[1]
            self.assertIn("name: %s" % name, front)
            self.assertIn("description:", front)


class HostInstallerHelpers(unittest.TestCase):
    def test_enable_grok_plugin_updates_existing_or_creates_new_config(self):
        tmp = Path(tempfile.mkdtemp(prefix="shuttle-grok-cfg-"))
        cfg = tmp / "config.toml"
        cfg.write_text('[ui]\nyolo = false\n\n[plugins]\nenabled = ["ponytail", "delegate"]\n', encoding="utf-8")
        D._enable_grok_plugin(cfg)
        self.assertIn('enabled = ["ponytail", "shuttle"]', cfg.read_text(encoding="utf-8"))
        self.assertNotIn('"delegate"', cfg.read_text(encoding="utf-8"))
        D._enable_grok_plugin(cfg)  # idempotent
        self.assertEqual(cfg.read_text(encoding="utf-8").count('"shuttle"'), 1)

    def test_install_opencode_writes_config_and_copies_skills(self):
        tmp = Path(tempfile.mkdtemp(prefix="shuttle-oc-cfg-"))
        saved = os.environ.get("XDG_CONFIG_HOME")
        try:
            os.environ["XDG_CONFIG_HOME"] = str(tmp)
            res = D.install("opencode")
            self.assertTrue(res[0]["installed"])
            cfg = json.loads((tmp / "opencode" / "opencode.json").read_text(encoding="utf-8"))
            self.assertIn("shuttle", cfg["mcp"]["servers"])
            for name in ("orchestrate", "shuttle", "shuttles", "shuttle-auth"):
                self.assertTrue((tmp / "opencode" / "skills" / name / "SKILL.md").exists())
        finally:
            if saved is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
