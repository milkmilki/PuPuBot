import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pupu.instance_context import (
    InstanceContext,
    activate_instance_context,
    get_current_instance_context,
)
from pupu.memory_index import memu_adapter
from pupu.persona.core import get_pupu_name
from pupu.storage.db import get_data_dir, get_db_path


class InstanceContextTests(unittest.TestCase):
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
                self.assertEqual(memu_adapter._memu_db_path(), inst_a / "data" / "memu.db")
                self.assertEqual(get_pupu_name(), "Alice")

            with activate_instance_context(ctx_b):
                self.assertEqual(get_current_instance_context(), ctx_b)
                self.assertEqual(get_db_path(), str(inst_b / "data" / "pupu.db"))
                self.assertEqual(get_data_dir(), str(inst_b / "data"))
                self.assertEqual(memu_adapter._memu_db_path(), inst_b / "data" / "memu.db")
                self.assertEqual(get_pupu_name(), "Bob")

    def test_memu_runtime_uses_context_specific_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst_a = self._make_instance(root, "aaa111", "Alice")
            inst_b = self._make_instance(root, "bbb222", "Bob")
            ctx_a = InstanceContext.from_instance_dir(inst_a)
            ctx_b = InstanceContext.from_instance_dir(inst_b)
            created: list[dict] = []

            with patch("pupu.memory_index.memu_adapter.is_memu_long_term_enabled", return_value=True):
                with patch("pupu.memory_index.memu_adapter._configured_embedding_key", return_value="key"):
                    with patch("pupu.memory_index.memu_adapter._log_config_once"):
                        with patch("pupu.memory_index.memu_adapter._new_service") as mock_new:
                            def _make_service():
                                service = {"service": len(created)}
                                created.append(service)
                                return service

                            mock_new.side_effect = _make_service
                            with activate_instance_context(ctx_a):
                                service_a1 = memu_adapter._get_service()
                                service_a2 = memu_adapter._get_service()
                            with activate_instance_context(ctx_b):
                                service_b1 = memu_adapter._get_service()

            self.assertIs(service_a1, service_a2)
            self.assertIsNot(service_a1, service_b1)
            self.assertEqual(mock_new.call_count, 2)


if __name__ == "__main__":
    unittest.main()
