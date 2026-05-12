import os
import tempfile
import unittest
from pathlib import Path

from pupu.instance_main import _ensure_instance_env


class InstanceMainTests(unittest.TestCase):
    def test_instance_env_overrides_global_memu_db_path(self) -> None:
        old_values = {
            key: os.environ.get(key)
            for key in (
                "PUPU_INSTANCE_DIR",
                "PUPU_CONFIG_PATH",
                "PUPU_DB_PATH",
                "PUPU_MEMU_DB_PATH",
                "PUPU_PERSONA_PATH",
            )
        }
        self.addCleanup(self._restore_env, old_values)

        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "instances" / "abc123"
            inst.mkdir(parents=True)
            os.environ["PUPU_MEMU_DB_PATH"] = "data/memu.db"

            _ensure_instance_env(inst)

            self.assertEqual(os.environ["PUPU_INSTANCE_DIR"], str(inst))
            self.assertEqual(os.environ["PUPU_MEMU_DB_PATH"], str(inst / "data" / "memu.db"))
            self.assertTrue((inst / "data").is_dir())

    @staticmethod
    def _restore_env(old_values: dict[str, str | None]) -> None:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
