import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class PersonaPathOverrideTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("PUPU_PERSONA_PATH", None)
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


if __name__ == "__main__":
    unittest.main()
