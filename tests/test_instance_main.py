import os
import tempfile
import unittest
from pathlib import Path

from pupu.instance_context import get_current_instance_context
from pupu.instance_main import _ensure_instance_env, _load_instance_dotenv


class InstanceMainTests(unittest.TestCase):
    def test_ensure_instance_activates_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "instances" / "abc123"
            inst.mkdir(parents=True)

            _ensure_instance_env(inst)

            ctx = get_current_instance_context()
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.instance_dir, inst.resolve())
            self.assertEqual(ctx.memu_db_path, inst.resolve() / "data" / "memu.db")
            self.assertTrue((inst / "data").is_dir())

    def test_load_instance_dotenv_exposes_instance_specific_values(self) -> None:
        old_values = {
            key: os.environ.get(key)
            for key in (
                "PUPU_TTS_REPLY_DEFAULT",
                "PUPU_PROACTIVE_ENABLED",
            )
        }
        self.addCleanup(self._restore_env, old_values)

        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "instances" / "abc123"
            inst.mkdir(parents=True)
            (inst / ".env.qq").write_text(
                "PUPU_TTS_REPLY_DEFAULT=true\n"
                "PUPU_PROACTIVE_ENABLED=false\n",
                encoding="utf-8",
            )

            _load_instance_dotenv(inst)

            self.assertEqual(os.environ["PUPU_TTS_REPLY_DEFAULT"], "true")
            self.assertEqual(os.environ["PUPU_PROACTIVE_ENABLED"], "false")

    @staticmethod
    def _restore_env(old_values: dict[str, str | None]) -> None:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
