import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pupu_console import instance_store, souls_store


def _write_minimal_messages_db(path: Path, marker: str | None = None) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT NOT NULL DEFAULT '')"
    )
    if marker is not None:
        conn.execute("INSERT INTO messages (content) VALUES (?)", (marker,))
    conn.commit()
    conn.close()


class ConsoleStoresTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_env = {
            key: os.environ.get(key)
            for key in ("PUPU_REPO_ROOT", "PUPU_YAML_PATH")
        }
        os.environ["PUPU_REPO_ROOT"] = self._tmpdir.name
        os.environ.pop("PUPU_YAML_PATH", None)

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_create_instance_and_roundtrip(self) -> None:
        iid = instance_store.create_instance("A", qq_mode="cli", port=8099)
        cfg, persona = instance_store.read_instance_files(iid)
        self.assertEqual(cfg["display_name"], "A")
        self.assertEqual(cfg["port"], 8099)
        self.assertEqual(cfg["qq_mode"], "cli")
        self.assertEqual(cfg["owner_ids"], [])
        self.assertEqual(cfg["private_reply_mode"], "owner_only")
        self.assertEqual(cfg["private_allowed_ids"], [])
        self.assertEqual(cfg["open_groups"], [])
        self.assertEqual(cfg["bot_id"], iid)
        self.assertNotIn("arbiter_url", cfg)
        self.assertNotIn("arbiter_base_url", cfg)
        self.assertEqual(cfg["peer"], {"bot_id": "", "name": "", "qq": "", "persona_brief": ""})
        self.assertGreaterEqual(cfg["debounce_seconds_open_group"], 5.0)
        self.assertTrue(cfg["proactive_enabled"])
        self.assertIn("core_persona", persona)
        port = instance_store.read_port(Path(self._tmpdir.name) / "instances" / iid)
        self.assertEqual(port, 8099)

    def test_create_instance_uses_yaml_defaults(self) -> None:
        yaml_path = Path(self._tmpdir.name) / "pupu.yaml"
        yaml_path.write_text(
            """
user:
  owner_ids: ["12345"]
napcat:
  host: 127.0.0.1
  port: 8123
  command_start: ["!"]
instance:
  display_name: YAML Bot
  qq_mode: napcat
""",
            encoding="utf-8",
        )
        os.environ["PUPU_YAML_PATH"] = str(yaml_path)

        iid = instance_store.create_instance("", qq_mode="napcat")
        cfg, _ = instance_store.read_instance_files(iid)
        self.assertEqual(cfg["display_name"], "YAML Bot")
        self.assertEqual(cfg["owner_ids"], ["12345"])
        self.assertEqual(cfg["private_reply_mode"], "owner_only")
        self.assertNotIn("qq_app_id", cfg)
        self.assertNotIn("qq_app_secret", cfg)
        self.assertEqual(cfg["port"], 8123)
        self.assertFalse((Path(self._tmpdir.name) / "instances" / iid / ".env.qq").exists())

    def test_proactive_enabled_normalizes_string_false(self) -> None:
        iid = instance_store.create_instance("A", qq_mode="cli", port=8099)
        inst_path = Path(self._tmpdir.name) / "instances" / iid / "instance.json"
        cfg = json.loads(inst_path.read_text(encoding="utf-8"))
        cfg["proactive_enabled"] = "false"
        inst_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        loaded, _ = instance_store.read_instance_files(iid)
        self.assertFalse(loaded["proactive_enabled"])

    def test_apply_soul_keeps_port_and_runtime_fields(self) -> None:
        iid = instance_store.create_instance("B", port=9101, qq_mode="napcat")
        instance_store.merge_update(
            iid,
            {
                "owner_ids": ["1", "2"],
            },
            None,
        )
        souls_dir = Path(self._tmpdir.name) / "souls"
        souls_dir.mkdir(parents=True, exist_ok=True)
        soul = {
            "slug": "s1",
            "display_name": "Soul",
            "name": "魂",
            "core_persona": "魂设",
            "seed_self_facts": {"x": "y"},
            "tool_servers": {
                "web": {"enabled": False},
                "filesystem": {"enabled": True},
                "system": {"enabled": True},
                "media": {"enabled": True},
                "scheduler": {"enabled": True},
            },
        }
        (souls_dir / "s1.json").write_text(
            json.dumps(soul, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        souls_store.apply_to_instance("s1", iid)
        cfg, persona = instance_store.read_instance_files(iid)
        self.assertEqual(cfg["port"], 9101)
        self.assertEqual(cfg["qq_mode"], "napcat")
        self.assertNotIn("qq_app_id", cfg)
        self.assertNotIn("qq_app_secret", cfg)
        self.assertEqual(cfg["owner_ids"], ["1", "2"])
        self.assertFalse(cfg["tool_servers"]["web"]["enabled"])
        self.assertEqual(persona["name"], "魂")
        self.assertEqual(persona["core_persona"], "魂设")
        self.assertEqual(persona["seed_self_facts"]["x"], "y")

    def test_capture_from_instance(self) -> None:
        iid = instance_store.create_instance("C", port=9200)
        instance_store.merge_update(
            iid,
            {"tool_servers": {"web": {"enabled": True}, "filesystem": {"enabled": True}, "system": {"enabled": True}, "media": {"enabled": True}, "scheduler": {"enabled": True}}},
            {"name": "C名", "core_persona": "CP", "seed_self_facts": {"a": "b"}},
        )
        souls_store.capture_from_instance(iid, "cap1", "预设展示")
        data = souls_store.load_soul("cap1")
        self.assertEqual(data["display_name"], "预设展示")
        self.assertEqual(data["name"], "C名")
        self.assertEqual(data["core_persona"], "CP")
        self.assertEqual(data["seed_self_facts"]["a"], "b")
        self.assertTrue(data["tool_servers"]["web"]["enabled"])

    def test_deprecated_instance_keys_scrubbed_on_write(self) -> None:
        iid = instance_store.create_instance("Z", port=9400)
        instance_store.merge_update(
            iid,
            {
                "mode": "play",
                "persona_enabled": True,
                "llm": {"p": 1},
                "qq_app_id": "old-app",
                "qq_app_secret": "old-secret",
            },
            None,
        )
        cfg, _ = instance_store.read_instance_files(iid)
        self.assertNotIn("mode", cfg)
        self.assertNotIn("persona_enabled", cfg)
        self.assertNotIn("llm", cfg)
        self.assertNotIn("qq_app_id", cfg)
        self.assertNotIn("qq_app_secret", cfg)

    def test_replace_memory_db_creates_target(self) -> None:
        iid = instance_store.create_instance("M1", port=9300)
        src = Path(self._tmpdir.name) / "src.db"
        _write_minimal_messages_db(src, "hi")
        target = instance_store.replace_memory_db(iid, src)
        self.assertTrue(target.is_file())
        conn = sqlite3.connect(str(target))
        try:
            row = conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
            self.assertEqual(row[0], "hi")
        finally:
            conn.close()

    def test_replace_memory_db_overwrites(self) -> None:
        iid = instance_store.create_instance("M2", port=9301)
        target = instance_store.memory_db_path(iid)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_minimal_messages_db(target, "old")
        src = Path(self._tmpdir.name) / "src2.db"
        _write_minimal_messages_db(src, "new")
        instance_store.replace_memory_db(iid, src)
        conn = sqlite3.connect(str(target))
        try:
            row = conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
            self.assertEqual(row[0], "new")
        finally:
            conn.close()

    def test_replace_memory_db_rejects_bad_file(self) -> None:
        iid = instance_store.create_instance("M3", port=9302)
        target = instance_store.memory_db_path(iid)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_minimal_messages_db(target, "keep")
        bad = Path(self._tmpdir.name) / "bad.bin"
        bad.write_bytes(b"not a sqlite file")
        with self.assertRaises(ValueError):
            instance_store.replace_memory_db(iid, bad)
        conn = sqlite3.connect(str(target))
        try:
            row = conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
            self.assertEqual(row[0], "keep")
        finally:
            conn.close()

    def test_replace_memory_db_requires_messages_table(self) -> None:
        iid = instance_store.create_instance("M4", port=9303)
        target = instance_store.memory_db_path(iid)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_minimal_messages_db(target, "keep")
        wrong = Path(self._tmpdir.name) / "nomsg.db"
        conn = sqlite3.connect(str(wrong))
        conn.execute("CREATE TABLE other (x int)")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            instance_store.replace_memory_db(iid, wrong)
        conn = sqlite3.connect(str(target))
        try:
            row = conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
            self.assertEqual(row[0], "keep")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
