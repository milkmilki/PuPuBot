import os
import sqlite3
from pathlib import Path
import unittest
from datetime import datetime
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.maintenance import (
    maybe_run_daily_maintenance,
    maybe_run_daily_memu_tidy,
    run_memory_maintenance,
)
from pupu.memory import (
    _get_conn,
    create_scheduled_task,
    get_event_thread_steps,
    get_self_facts,
    get_user_facts,
    init_db,
    reset_session,
    save_message,
    save_summary,
    upsert_event_threads,
    upsert_self_facts,
    upsert_user_facts,
)
from pupu.message_sources import CHAT, PROACTIVE
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
                "event_steps",
                "messages",
                "familiarity",
                "events",
                "event_threads",
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
        save_message("user", f"user-{index}", self.session_id, source=CHAT)
        save_message("assistant", f"assistant-{index}", self.session_id, source=CHAT)

    def test_run_memory_maintenance_dedupes_and_prunes(self):
        for i in range(14):
            self._save_chat_turn(i)

        for i in range(25):
            save_message("assistant", f"proactive-{i}", self.session_id, source=PROACTIVE)

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

    def test_maybe_run_daily_memu_tidy_runs_once_during_three_oclock_hour(self):
        tidy_result = {
            "mode": "apply",
            "scanned": 3,
            "candidates": 1,
            "deleted": 1,
            "failed": 0,
            "source_deleted": 0,
            "local_deleted": 0,
            "updated": 0,
            "reason_counts": {"过期": 1},
            "scanned_kind_counts": {"event_thread": 3},
            "drop_kind_counts": {"event_thread": 1},
            "judge_notes": ["ok"],
            "judge_failures": 0,
            "unknown_drop_ids": 0,
            "note": "done",
            "status": "ok",
        }
        with patch("pupu.maintenance.run_memu_tidy", return_value=tidy_result) as mock_run:
            self.assertIsNone(maybe_run_daily_memu_tidy(datetime(2026, 4, 26, 2, 59, 0)))
            report = maybe_run_daily_memu_tidy(datetime(2026, 4, 26, 3, 1, 0))
            self.assertIn("memU tidy complete", report)
            mock_run.assert_called_once()

        conn = _get_conn()
        try:
            count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM maintenance_runs
                   WHERE run_date = ? AND trigger = ? AND status = ?""",
                ("2026-04-26", "auto_memu_tidy", "success"),
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(count, 1)

        with patch("pupu.maintenance.run_memu_tidy", return_value=tidy_result) as mock_run:
            self.assertIsNone(maybe_run_daily_memu_tidy(datetime(2026, 4, 26, 8, 0, 0)))
            mock_run.assert_not_called()

        with patch("pupu.maintenance.run_memu_tidy", return_value=tidy_result) as mock_run:
            self.assertIsNone(maybe_run_daily_memu_tidy(datetime(2026, 4, 27, 21, 0, 0)))
            mock_run.assert_not_called()

    def test_run_memory_maintenance_forwards_memu_mode(self):
        run_at = datetime(2026, 4, 26, 3, 0, 0)
        with patch("pupu.maintenance._run_memory_maintenance", return_value="ok") as mock_run:
            report = run_memory_maintenance(
                trigger="manual",
            include_model=False,
            now=run_at,
            memu_mode="check",
        )

        self.assertEqual(report, "ok")
        mock_run.assert_called_once_with(
            trigger="manual",
            include_model=False,
            now=run_at,
            memu_mode="check",
        )

    def test_run_memory_maintenance_check_uses_model_preview(self):
        for i in range(6):
            self._save_chat_turn(i)

        with patch(
            "pupu.maintenance.runner._run_model_compaction",
            return_value={
                "dropped_summaries": 0,
                "merged_summaries": 0,
                "updated_event_threads": 0,
                "deleted_facts": 0,
                "updated_facts": 0,
                "note": "",
            },
        ) as mock_model:
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
                memu_mode="check",
            )

        self.assertIn("记忆整理检查完成（manual）", report)
        mock_model.assert_called()
        self.assertTrue(all(call.kwargs.get("apply") is False for call in mock_model.call_args_list))

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

    def test_maintenance_commits_before_model_compaction(self):
        for i in range(8):
            self._save_chat_turn(i)

        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? ORDER BY id ASC",
                (self.session_id,),
            ).fetchall()
            start_msg_id = int(rows[0]["id"])
            end_msg_id = int(rows[-1]["id"])
        finally:
            conn.close()

        save_summary("duplicate-a", start_msg_id, end_msg_id, self.session_id)
        save_summary("duplicate-b", start_msg_id, end_msg_id, self.session_id)

        def _second_connection_write(conn, snapshot):
            other = sqlite3.connect(str(TEST_DB_PATH), timeout=0.2)
            try:
                other.execute(
                    """INSERT INTO messages (session_id, role, content, timestamp, source)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        self.session_id,
                        "assistant",
                        "write while maintenance model waits",
                        "2026-04-26T03:00:00",
                        "proactive",
                    ),
                )
                other.commit()
            finally:
                other.close()
            return {
                "dropped_summaries": 0,
                "merged_summaries": 0,
                "updated_event_threads": 0,
                "deleted_facts": 0,
                "updated_facts": 0,
                "note": "",
            }

        with patch(
            "pupu.maintenance.runner._run_model_compaction",
            side_effect=_second_connection_write,
        ):
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        self.assertIn("记忆整理完成（manual）", report)

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

    def test_model_maintenance_updates_event_threads_without_dropping(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "movie-plan",
                    "title": "一起看电影",
                    "kind": "promise",
                    "event_time": "2026-05-01",
                    "time_text": "五一",
                    "details": "用户和仆仆约好一起看电影",
                    "followup_hint": "之后可以提起这件事",
                    "confidence": 0.8,
                },
                {
                    "thread_key": "stale-plan",
                    "title": "过期小事",
                    "kind": "note",
                    "details": "一次性小事",
                    "confidence": 0.3,
                },
            ],
        )
        conn = _get_conn()
        try:
            thread_ids = {
                row["key"]: int(row["id"])
                for row in conn.execute(
                    "SELECT id, key FROM event_threads WHERE session_id = ?",
                    (self.session_id,),
                ).fetchall()
            }
        finally:
            conn.close()

        raw = f"""{{
          "drop_ids": [{thread_ids["stale-plan"]}],
          "updates": [
            {{
              "id": {thread_ids["movie-plan"]},
              "title": "五一看电影约定",
              "details": "用户和仆仆约定五一一起看电影，之后可以自然跟进。",
              "confidence": 0.95
            }}
          ],
          "notes": "保留更重要的约定"
        }}"""

        with patch("pupu.maintenance.model_compaction.json_task", return_value=raw):
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        kept, kept_steps = get_event_thread_steps(self.session_id, "movie-plan")
        stale, stale_steps = get_event_thread_steps(self.session_id, "stale-plan")

        self.assertEqual(kept["title"], "五一看电影约定")
        self.assertEqual(kept["time_text"], "五一")
        self.assertEqual(kept["followup_hint"], "之后可以提起这件事")
        self.assertEqual(kept["confidence"], 0.95)
        self.assertIn("五一一起看电影", kept_steps[-1]["summary"])
        self.assertEqual(stale["status"], "active")
        self.assertEqual(len(stale_steps), 1)
        self.assertNotIn("模型删除事件线", report)
        self.assertIn("- 模型更新事件线：1", report)


if __name__ == "__main__":
    unittest.main()
