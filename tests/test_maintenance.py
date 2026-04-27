import os
from pathlib import Path
import unittest
from datetime import datetime
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.maintenance import maybe_run_daily_maintenance, run_memory_maintenance
from pupu.memory import (
    _get_conn,
    create_scheduled_task,
    get_self_facts,
    get_user_facts,
    init_db,
    reset_session,
    save_message,
    save_summary,
    upsert_self_facts,
    upsert_user_facts,
)
from pupu.maintenance.model_compaction import _call_model_json


class MaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = "test_maintenance"
        conn = _get_conn()
        try:
            for table in (
                "messages",
                "familiarity",
                "events",
                "important_events",
                "user_facts",
                "summaries",
                "self_facts",
                "scheduled_tasks",
                "maintenance_runs",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()
        reset_session(self.session_id)

    def _save_chat_turn(self, index: int):
        save_message("user", f"user-{index}", self.session_id, source="chat")
        save_message("assistant", f"assistant-{index}", self.session_id, source="chat")

    def test_run_memory_maintenance_dedupes_and_prunes(self):
        for i in range(14):
            self._save_chat_turn(i)

        for i in range(25):
            save_message("assistant", f"proactive-{i}", self.session_id, source="proactive")

        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? ORDER BY id ASC",
                (self.session_id,),
            ).fetchall()
            first_msg_id = int(rows[0]["id"])
            tenth_turn_end_id = int(rows[19]["id"])
            save_summary("summary-a", first_msg_id, tenth_turn_end_id, self.session_id)
            save_summary("summary-b", first_msg_id, tenth_turn_end_id, self.session_id)

            conn.execute(
                """INSERT INTO events (session_id, date, delta, description)
                   VALUES (?, ?, ?, ?)""",
                (self.session_id, "2026-04-26T00:00:00", 2, "same event"),
            )
            conn.execute(
                """INSERT INTO events (session_id, date, delta, description)
                   VALUES (?, ?, ?, ?)""",
                (self.session_id, "2026-04-26T00:00:00", 2, "same event"),
            )
            conn.execute(
                """INSERT INTO important_events
                   (session_id, source_event_key, title, kind, event_time, time_text,
                    details, followup_hint, confidence, status, linked_task_id, last_seen_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.session_id,
                    "event-a",
                    "一起看电影",
                    "promise",
                    "2026-05-01",
                    "五一",
                    "约好一起看电影",
                    "之后可以提起这件事",
                    0.8,
                    "active",
                    None,
                    "2026-04-26T00:00:00",
                    "2026-04-26T00:00:00",
                ),
            )
            conn.execute(
                """INSERT INTO important_events
                   (session_id, source_event_key, title, kind, event_time, time_text,
                    details, followup_hint, confidence, status, linked_task_id, last_seen_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.session_id,
                    "event-b",
                    "一起看电影",
                    "promise",
                    "2026-05-01",
                    "五一",
                    "约好一起看电影",
                    "之后可以提起这件事",
                    0.7,
                    "active",
                    None,
                    "2026-04-26T00:00:00",
                    "2026-04-26T00:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        create_scheduled_task(
            self.session_id,
            "提醒",
            "看电影",
            "2026-05-01T20:00:00",
            "once",
            None,
        )
        create_scheduled_task(
            self.session_id,
            "提醒",
            "看电影",
            "2026-05-01T20:00:00",
            "once",
            None,
        )

        report = run_memory_maintenance(
            trigger="manual",
            include_model=False,
            now=datetime(2026, 4, 26, 3, 0, 0),
        )

        self.assertIn("记忆整理完成（manual）", report)
        self.assertIn("- 去重摘要：1", report)
        self.assertIn("- 去重旧好感度记录：1", report)
        self.assertIn("- 去重重要事件：1", report)
        self.assertIn("- 去重定时任务：1", report)
        self.assertIn("- 清理旧聊天消息：4", report)
        self.assertIn("- 清理旧内部消息：5", report)

        conn = _get_conn()
        try:
            summary_count = conn.execute(
                "SELECT COUNT(*) AS c FROM summaries WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["c"]
            enabled_task_count = conn.execute(
                "SELECT COUNT(*) AS c FROM scheduled_tasks WHERE session_id = ? AND enabled = 1",
                (self.session_id,),
            ).fetchone()["c"]
            important_event_count = conn.execute(
                "SELECT COUNT(*) AS c FROM important_events WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["c"]
            proactive_count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM messages
                   WHERE session_id = ? AND source = 'proactive'""",
                (self.session_id,),
            ).fetchone()["c"]
            chat_count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM messages
                   WHERE session_id = ? AND source = 'chat'""",
                (self.session_id,),
            ).fetchone()["c"]
            maintenance_count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM maintenance_runs
                   WHERE run_date = '2026-04-26' AND trigger = 'manual' AND status = 'success'""",
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(summary_count, 1)
        self.assertEqual(enabled_task_count, 1)
        self.assertEqual(important_event_count, 1)
        self.assertEqual(proactive_count, 20)
        self.assertEqual(chat_count, 24)
        self.assertEqual(maintenance_count, 1)

    def test_maybe_run_daily_maintenance_runs_once_after_three(self):
        with patch("pupu.maintenance.run_memory_maintenance", return_value="ok") as mock_run:
            self.assertIsNone(maybe_run_daily_maintenance(datetime(2026, 4, 26, 2, 59, 0)))
            self.assertEqual(
                maybe_run_daily_maintenance(datetime(2026, 4, 26, 3, 1, 0)),
                "ok",
            )
            mock_run.assert_called_once()

        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO maintenance_runs
                   (run_date, trigger, status, report, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "2026-04-26",
                    "auto",
                    "success",
                    "done",
                    "2026-04-26T03:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with patch("pupu.maintenance.run_memory_maintenance", return_value="should-not-run") as mock_run:
            self.assertIsNone(maybe_run_daily_maintenance(datetime(2026, 4, 26, 8, 0, 0)))
            mock_run.assert_not_called()

    def test_maintenance_prunes_old_disabled_scheduled_tasks(self):
        old_disabled_id = create_scheduled_task(
            self.session_id,
            "old disabled",
            "old disabled instruction",
            "2026-05-01T09:00:00",
            "once",
            None,
        )
        recent_disabled_id = create_scheduled_task(
            self.session_id,
            "recent disabled",
            "recent disabled instruction",
            "2026-05-01T10:00:00",
            "once",
            None,
        )
        active_old_id = create_scheduled_task(
            self.session_id,
            "active old",
            "active old instruction",
            "2026-05-01T11:00:00",
            "once",
            None,
        )

        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE scheduled_tasks SET enabled = 0, created_at = ? WHERE id = ?",
                ("2026-03-01T00:00:00", old_disabled_id),
            )
            conn.execute(
                "UPDATE scheduled_tasks SET enabled = 0, created_at = ? WHERE id = ?",
                ("2026-04-20T00:00:00", recent_disabled_id),
            )
            conn.execute(
                "UPDATE scheduled_tasks SET created_at = ? WHERE id = ?",
                ("2026-03-01T00:00:00", active_old_id),
            )
            conn.commit()
        finally:
            conn.close()

        run_memory_maintenance(
            trigger="manual",
            include_model=False,
            now=datetime(2026, 4, 26, 3, 0, 0),
        )

        conn = _get_conn()
        try:
            ids = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM scheduled_tasks WHERE session_id = ?",
                    (self.session_id,),
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertNotIn(old_disabled_id, ids)
        self.assertIn(recent_disabled_id, ids)
        self.assertIn(active_old_id, ids)

    def test_model_compaction_json_tasks_route_to_maintenance_provider(self):
        with patch(
            "pupu.maintenance.model_compaction.json_task",
            return_value='{"drop_summary_ids":[],"merged_summary":"","notes":""}',
        ) as mock_json_task:
            result = _call_model_json("system prompt", {"x": 1}, task_name="unit")

        self.assertEqual(result["drop_summary_ids"], [])
        self.assertEqual(mock_json_task.call_args.kwargs["role"], "maintenance")
        self.assertEqual(mock_json_task.call_args.kwargs["task_name"], "unit")

    def test_model_maintenance_compacts_user_and_self_facts(self):
        upsert_user_facts(
            {
                "称呼偏好": "用户称仆仆为姐姐",
                "nickname_for_pupu": "曾称呼仆仆为“姐姐”",
                "身份": "读研学生",
            },
            self.session_id,
        )
        upsert_self_facts(
            {
                "自称": "姐姐",
                "仆仆自称": "姐姐",
                "喜欢的游戏": "喜欢独立游戏",
            },
            self.session_id,
        )

        raw = """{
          "user_updates": {
            "称呼偏好": "用户习惯称仆仆为姐姐"
          },
          "user_delete_keys": ["nickname_for_pupu"],
          "self_updates": {
            "自称": "姐姐"
          },
          "self_delete_keys": ["仆仆自称"],
          "notes": "合并重复称呼事实"
        }"""

        with patch("pupu.maintenance.model_compaction.json_task", return_value=raw) as mock_json:
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        user_facts = get_user_facts(self.session_id)
        self_facts = get_self_facts(self.session_id)

        self.assertEqual(mock_json.call_args.kwargs["task_name"], "facts_maintenance")
        self.assertEqual(user_facts["称呼偏好"], "用户习惯称仆仆为姐姐")
        self.assertNotIn("nickname_for_pupu", user_facts)
        self.assertEqual(user_facts["身份"], "读研学生")
        self.assertEqual(self_facts["自称"], "姐姐")
        self.assertNotIn("仆仆自称", self_facts)
        self.assertEqual(self_facts["喜欢的游戏"], "喜欢独立游戏")
        self.assertIn("- 模型删除事实：2", report)
        self.assertIn("- 模型更新事实：1", report)


if __name__ == "__main__":
    unittest.main()
