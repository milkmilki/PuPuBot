import importlib
import os
import tempfile
import unittest
from pathlib import Path


class AppConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PUPU_REPO_ROOT",
                "PUPU_YAML_PATH",
                "PUPU_CHAT_PROVIDER",
                "PUPU_JUDGE_PROVIDER",
                "PUPU_DEEPSEEK_API_KEY",
                "PUPU_DEEPSEEK_BASE_URL",
                "PUPU_CONSOLE_PORT",
                "PUPU_ARBITER_DEBOUNCE_IDLE_SEC",
                "PUPU_MEMU_ENABLED",
                "PUPU_TTS_ENABLED",
                "PUPU_CODEX_MCP_SERVERS_JSON",
                "PUPU_MCP_SERVERS_JSON",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_yaml(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        with tmp:
            tmp.write(text)
        os.environ["PUPU_YAML_PATH"] = tmp.name
        return Path(tmp.name)

    def test_apply_app_config_env_maps_common_fields(self) -> None:
        self._write_yaml(
            """
llm:
  provider: deepseek
  deepseek:
    api_key: test-key
    base_url: https://deepseek.test/anthropic
console:
  port: 8999
arbiter:
  debounce_idle_seconds: 12
memu:
  enabled: false
tts:
  enabled: true
mcp:
  servers:
    brave-search:
      enabled: true
      command: npx
      args: ["-y", "@modelcontextprotocol/server-brave-search"]
      exposures: ["chat", "proactive"]
      timeout: 30
      env:
        BRAVE_API_KEY: test-brave-key
    disabled-demo:
      enabled: false
      command: npx
"""
        )
        from pupu import app_config

        app_config.apply_app_config_env(override=True)

        self.assertEqual(os.environ["PUPU_CHAT_PROVIDER"], "deepseek")
        self.assertEqual(os.environ["PUPU_JUDGE_PROVIDER"], "deepseek")
        self.assertEqual(os.environ["PUPU_DEEPSEEK_API_KEY"], "test-key")
        self.assertEqual(os.environ["PUPU_DEEPSEEK_BASE_URL"], "https://deepseek.test/anthropic")
        self.assertEqual(os.environ["PUPU_CONSOLE_PORT"], "8999")
        self.assertEqual(os.environ["PUPU_ARBITER_DEBOUNCE_IDLE_SEC"], "12")
        self.assertEqual(os.environ["PUPU_MEMU_ENABLED"], "false")
        self.assertEqual(os.environ["PUPU_TTS_ENABLED"], "true")
        self.assertIn("brave-search", os.environ["PUPU_CODEX_MCP_SERVERS_JSON"])
        self.assertIn("test-brave-key", os.environ["PUPU_CODEX_MCP_SERVERS_JSON"])
        self.assertNotIn("disabled-demo", os.environ["PUPU_CODEX_MCP_SERVERS_JSON"])
        self.assertEqual(
            os.environ["PUPU_MCP_SERVERS_JSON"],
            os.environ["PUPU_CODEX_MCP_SERVERS_JSON"],
        )
        self.assertIn('"exposures": ["chat", "proactive"]', os.environ["PUPU_MCP_SERVERS_JSON"])
        self.assertIn('"timeout": "30"', os.environ["PUPU_MCP_SERVERS_JSON"])

    def test_ensure_app_config_file_creates_from_template_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pupu.yaml.example").write_text(
                "llm:\n  provider: deepseek\n",
                encoding="utf-8",
            )
            os.environ["PUPU_REPO_ROOT"] = str(root)
            os.environ.pop("PUPU_YAML_PATH", None)

            from pupu import app_config

            path, created = app_config.ensure_app_config_file()
            self.assertTrue(created)
            self.assertEqual(path, root / "pupu.yaml")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "llm:\n  provider: deepseek\n",
            )

            path.write_text("custom: true\n", encoding="utf-8")
            second_path, second_created = app_config.ensure_app_config_file()
            self.assertFalse(second_created)
            self.assertEqual(second_path, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "custom: true\n")

    def test_default_instance_and_env_qq_use_yaml(self) -> None:
        self._write_yaml(
            """
user:
  owner_ids: [12345, "67890"]
instance:
  display_name: Lulu
  qq_mode: napcat
  qq_app_id: app-id
  qq_app_secret: app-secret
napcat:
  host: 127.0.0.1
  port: 8123
  command_start: ["!", "/"]
  command_sep: ["."]
"""
        )
        from pupu import app_config

        defaults = app_config.default_instance_settings()
        self.assertEqual(defaults["display_name"], "Lulu")
        self.assertEqual(defaults["qq_mode"], "napcat")
        self.assertEqual(defaults["qq_app_id"], "app-id")
        self.assertEqual(defaults["qq_app_secret"], "app-secret")
        self.assertEqual(defaults["owner_ids"], ["12345", "67890"])
        self.assertEqual(defaults["port"], 8123)

        env_text = app_config.format_env_qq()
        self.assertIn("HOST=127.0.0.1", env_text)
        self.assertIn("PORT=8123", env_text)
        self.assertIn('COMMAND_START=["!", "/"]', env_text)

    def test_config_owner_defaults_fall_back_to_yaml(self) -> None:
        self._write_yaml(
            """
user:
  owner_ids: ["2468"]
"""
        )
        os.environ.pop("PUPU_CONFIG_PATH", None)
        import pupu.config as cfg

        importlib.reload(cfg)
        self.assertEqual(cfg.load_owner_ids(), ["2468"])


if __name__ == "__main__":
    unittest.main()
