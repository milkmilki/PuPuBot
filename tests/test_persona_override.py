import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class PersonaPathOverrideTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("PUPU_PERSONA_PATH", None)
        os.environ.pop("PUPU_CONFIG_PATH", None)
        os.environ.pop("PUPU_INSTANCE_DIR", None)
        import pupu.persona.core as core

        importlib.reload(core)

    def test_getters_read_json_file(self) -> None:
        payload = {
            "name": "小测",
            "core_persona": "你是测试人格。",
            "seed_self_facts": {"k": "v"},
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(payload, f)
            path = f.name
        try:
            os.environ["PUPU_PERSONA_PATH"] = path
            import pupu.persona.core as core

            importlib.reload(core)
            self.assertEqual(core.get_pupu_name(), "小测")
            self.assertEqual(core.get_core_persona(), "你是测试人格。")
            self.assertEqual(core.get_seed_self_facts(), {"k": "v"})
        finally:
            Path(path).unlink(missing_ok=True)

    def test_display_name_overrides_default_persona_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persona_path = root / "persona.json"
            config_path = root / "instance.json"
            persona_path.write_text(
                json.dumps(
                    {
                        "name": "仆仆",
                        "core_persona": "你叫璐璐，是一个温柔开朗的女生。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps({"display_name": "璐璐"}, ensure_ascii=False),
                encoding="utf-8",
            )

            os.environ["PUPU_PERSONA_PATH"] = str(persona_path)
            os.environ["PUPU_CONFIG_PATH"] = str(config_path)
            import pupu.persona.core as core

            importlib.reload(core)
            self.assertEqual(core.get_pupu_name(), "璐璐")

    def test_core_persona_name_used_when_default_name_is_stale(self) -> None:
        payload = {
            "name": "仆仆",
            "core_persona": "你叫璐璐，是一个温柔开朗的女生。",
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(payload, f, ensure_ascii=False)
            path = f.name
        try:
            os.environ["PUPU_PERSONA_PATH"] = path
            import pupu.persona.core as core

            importlib.reload(core)
            self.assertEqual(core.get_pupu_name(), "璐璐")
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
