import importlib
import json
import tempfile
import unittest
from pathlib import Path

from pupu.instance_context import InstanceContext, activate_instance_context


class PersonaPathOverrideTests(unittest.TestCase):
    def _make_instance(
        self,
        root: Path,
        *,
        persona: dict,
        config: dict | None = None,
    ) -> Path:
        inst = root / "instances" / "abc123"
        (inst / "data").mkdir(parents=True)
        (inst / "persona.json").write_text(
            json.dumps(persona, ensure_ascii=False),
            encoding="utf-8",
        )
        (inst / "instance.json").write_text(
            json.dumps(config or {}, ensure_ascii=False),
            encoding="utf-8",
        )
        return inst

    def test_getters_read_instance_persona_json(self) -> None:
        payload = {
            "name": "小测",
            "core_persona": "你是测试人格。",
            "seed_self_facts": {"k": "v"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._make_instance(Path(tmp), persona=payload)
            import pupu.persona.core as core

            importlib.reload(core)
            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(core.get_pupu_name(), "小测")
                self.assertEqual(core.get_core_persona(), "你是测试人格。")
                self.assertEqual(core.get_seed_self_facts(), {"k": "v"})

    def test_display_name_overrides_default_persona_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._make_instance(
                Path(tmp),
                persona={
                    "name": "仆仆",
                    "core_persona": "你叫璐璐，是一个温柔开朗的女生。",
                },
                config={"display_name": "璐璐"},
            )
            import pupu.persona.core as core

            importlib.reload(core)
            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(core.get_pupu_name(), "璐璐")

    def test_core_persona_name_used_when_default_name_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._make_instance(
                Path(tmp),
                persona={
                    "name": "仆仆",
                    "core_persona": "你叫璐璐，是一个温柔开朗的女生。",
                },
            )
            import pupu.persona.core as core

            importlib.reload(core)
            with activate_instance_context(InstanceContext.from_instance_dir(inst)):
                self.assertEqual(core.get_pupu_name(), "璐璐")


if __name__ == "__main__":
    unittest.main()
