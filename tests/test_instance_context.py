import json
import tempfile
import unittest
from pathlib import Path

from pupu.instance_context import (
    InstanceContext,
    activate_instance_context,
    get_current_instance_context,
)
from pupu.logging_utils import close_all_log_sinks
from pupu.persona.core import get_pupu_name
from pupu.storage.db import get_data_dir, get_db_path


class InstanceContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        close_all_log_sinks()

    def _make_instance(self, root: Path, instance_id: str, display_name: str) -> Path:
        inst = root / "instances" / instance_id
        inst.mkdir(parents=True)
        (inst / "data").mkdir()
        (inst / "instance.json").write_text(
            json.dumps(
                {"display_name": display_name, "qq_mode": "cli"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (inst / "persona.json").write_text(
            json.dumps({"name": display_name}, ensure_ascii=False),
            encoding="utf-8",
        )
        return inst

    def test_context_paths_are_instance_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst_a = self._make_instance(root, "aaa111", "Alice")
            inst_b = self._make_instance(root, "bbb222", "Bob")

            ctx_a = InstanceContext.from_instance_dir(inst_a)
            ctx_b = InstanceContext.from_instance_dir(inst_b)

            with activate_instance_context(ctx_a):
                self.assertEqual(get_current_instance_context(), ctx_a)
                self.assertEqual(get_db_path(), str(inst_a / "data" / "pupu.db"))
                self.assertEqual(get_data_dir(), str(inst_a / "data"))
                self.assertEqual(get_pupu_name(), "Alice")

            with activate_instance_context(ctx_b):
                self.assertEqual(get_current_instance_context(), ctx_b)
                self.assertEqual(get_db_path(), str(inst_b / "data" / "pupu.db"))
                self.assertEqual(get_data_dir(), str(inst_b / "data"))
                self.assertEqual(get_pupu_name(), "Bob")


if __name__ == "__main__":
    unittest.main()
