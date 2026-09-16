import os
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests.helpers import activate_test_instance
from pupu.instance_context import InstanceContext, activate_instance_context

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"

from pupu.maintenance import (
    maybe_run_daily_maintenance,
    maybe_run_daily_semantic_tidy,
    run_memory_maintenance,
)
from pupu.maintenance.model_compaction import _call_model_json
from pupu.maintenance.runner import _maintenance_lock_for_current_instance
from pupu.memory import (
    _get_conn,
    append_event_step,
    create_scheduled_task,
    get_event_thread_by_key,
    get_event_thread_steps,
    get_person_fact_map,
    init_db,
    reset_session,
    save_message,
    save_summary,
    update_person_fact_by_id,
    upsert_event_threads,
    upsert_person_facts,
)
from pupu.message_sources import CHAT, PROACTIVE
from pupu.storage.people import INSTANCE_PERSON_KEY, person_from_session


class MaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = "test_maintenance"
        os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"
        os.environ["PUPU_MEMORY_MAINTENANCE_ENABLED"] = "true"
        os.environ.pop("PUPU_MEMORY_MAINTENANCE_HOUR", None)
        conn = _get_conn()
        try:
            for table in (
                "memory_mutations",
                "event_thread_aliases",
                "event_thread_rollups",
                "event_people",
                "event_steps",
                "messages",
                "archived_messages",
                "familiarity",
                "events",
                "event_threads",
                "summaries",
                "scheduled_tasks",
                "maintenance_runs",
                "fact_evidence",
                "person_facts",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()
        reset_session(self.session_id)

    def _save_chat_turn(self, index: int):
        save_message("user", f"user-{index}", self.session_id, source=CHAT)
        save_message("assistant", f"assistant-{index}", self.session_id, source=CHAT)

    def test_maintenance_locks_are_isolated_by_instance_database(self):
        base = TEST_DB_PATH.parent
        context_a = InstanceContext.from_instance_dir(base / "maintenance-lock-a")
        context_b = InstanceContext.from_instance_dir(base / "maintenance-lock-b")

        with activate_instance_context(context_a):
            lock_a = _maintenance_lock_for_current_instance()
            self.assertIs(lock_a, _maintenance_lock_for_current_instance())
        with activate_instance_context(context_b):
            lock_b = _maintenance_lock_for_current_instance()

        self.assertIsNot(lock_a, lock_b)
        self.assertTrue(lock_a.acquire(blocking=False))
        try:
            self.assertTrue(lock_b.acquire(blocking=False))
            lock_b.release()
        finally:
            lock_a.release()

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
        self.assertIn("- 归档旧聊天消息：4", report)
        self.assertIn("- 归档旧内部消息：5", report)

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
            archived_count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM archived_messages
                   WHERE session_id = ?""",
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
        self.assertEqual(archived_count, 9)
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

    def test_daily_maintenance_respects_enabled_and_hour(self):
        with patch(
            "pupu.maintenance.run_memory_maintenance",
            return_value="ok",
        ) as mock_run:
            with patch.dict(
                os.environ,
                {"PUPU_MEMORY_MAINTENANCE_ENABLED": "false"},
                clear=False,
            ):
                self.assertIsNone(
                    maybe_run_daily_maintenance(
                        datetime(2026, 4, 26, 12, 0, 0)
                    )
                )
            with patch.dict(
                os.environ,
                {
                    "PUPU_MEMORY_MAINTENANCE_ENABLED": "true",
                    "PUPU_MEMORY_MAINTENANCE_HOUR": "5",
                },
                clear=False,
            ):
                self.assertIsNone(
                    maybe_run_daily_maintenance(
                        datetime(2026, 4, 26, 4, 59, 0)
                    )
                )
                self.assertEqual(
                    maybe_run_daily_maintenance(
                        datetime(2026, 4, 26, 5, 0, 0)
                    ),
                    "ok",
                )
        mock_run.assert_called_once()

    def test_maybe_run_daily_semantic_tidy_runs_once_during_three_oclock_hour(self):
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
        with patch("pupu.maintenance.run_semantic_tidy", return_value=tidy_result) as mock_run:
            self.assertIsNone(maybe_run_daily_semantic_tidy(datetime(2026, 4, 26, 2, 59, 0)))
            report = maybe_run_daily_semantic_tidy(datetime(2026, 4, 26, 3, 1, 0))
            self.assertIn("semantic index sync complete", report)
            mock_run.assert_called_once()

        conn = _get_conn()
        try:
            count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM maintenance_runs
                   WHERE run_date = ? AND trigger = ? AND status = ?""",
                ("2026-04-26", "auto_semantic_tidy", "success"),
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(count, 1)

        with patch("pupu.maintenance.run_semantic_tidy", return_value=tidy_result) as mock_run:
            self.assertIsNone(maybe_run_daily_semantic_tidy(datetime(2026, 4, 26, 8, 0, 0)))
            mock_run.assert_not_called()

        with patch("pupu.maintenance.run_semantic_tidy", return_value=tidy_result) as mock_run:
            self.assertIsNone(maybe_run_daily_semantic_tidy(datetime(2026, 4, 27, 21, 0, 0)))
            mock_run.assert_not_called()

    def test_run_memory_maintenance_forwards_semantic_mode(self):
        run_at = datetime(2026, 4, 26, 3, 0, 0)
        with patch("pupu.maintenance._run_memory_maintenance", return_value="ok") as mock_run:
            report = run_memory_maintenance(
                trigger="manual",
            include_model=False,
            now=run_at,
            semantic_mode="check",
        )

        self.assertEqual(report, "ok")
        mock_run.assert_called_once_with(
            trigger="manual",
            include_model=False,
            now=run_at,
            semantic_mode="check",
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
                semantic_mode="check",
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
            "pupu.maintenance.model_call.json_task",
            return_value='{"drop_summary_ids":[],"merged_summary":"","notes":""}',
        ) as mock_json_task:
            result = _call_model_json("system prompt", {"x": 1}, task_name="unit")

        self.assertEqual(result["drop_summary_ids"], [])
        self.assertEqual(mock_json_task.call_args.kwargs["role"], "maintenance")
        self.assertEqual(mock_json_task.call_args.kwargs["task_name"], "unit")

    def test_model_compaction_logs_raw_response_when_json_parse_fails(self):
        raw_response = "upstream overloaded: retry later"
        log_path = Path("/tmp/maintenance-model-errors-20260426.jsonl")
        with (
            patch(
                "pupu.maintenance.model_call.json_task",
                return_value=raw_response,
            ),
            patch(
                "pupu.maintenance.model_call.last_provider_label",
                return_value="deepseek:test",
            ),
            patch(
                "pupu.maintenance.model_call.record_model_response_error",
                return_value=log_path,
            ) as mock_record,
        ):
            with self.assertRaises(ValueError) as raised:
                _call_model_json("system prompt", {"x": 1}, task_name="unit")

        self.assertIn("raw_response_chars=32", str(raised.exception))
        self.assertIn(f"raw_response_log={log_path}", str(raised.exception))

        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["task_name"], "unit")
        self.assertEqual(mock_record.call_args.kwargs["provider"], "deepseek:test")
        self.assertEqual(mock_record.call_args.kwargs["raw_response"], raw_response)

    def test_model_compaction_preserves_parse_error_when_raw_log_write_fails(self):
        with (
            patch(
                "pupu.maintenance.model_call.json_task",
                return_value="<html>bad gateway</html>",
            ),
            patch(
                "pupu.maintenance.model_call.record_model_response_error",
                side_effect=OSError("disk full"),
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                r"raw_response_log=unavailable \(OSError: disk full\)",
            ):
                _call_model_json("system prompt", {"x": 1}, task_name="unit")

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

    def test_model_maintenance_compacts_owner_and_instance_facts(self):
        owner_key = person_from_session(self.session_id)
        upsert_person_facts(
            {
                "称呼偏好": "用户称仆仆为姐姐",
                "nickname_for_pupu": "曾称呼仆仆为“姐姐”",
                "身份": "读研学生",
            },
            default_subject_person_key=owner_key,
            context_session=self.session_id,
        )
        upsert_person_facts(
            {
                "自称": "姐姐",
                "仆仆自称": "姐姐",
                "喜欢的游戏": "喜欢独立游戏",
            },
            default_subject_person_key=INSTANCE_PERSON_KEY,
            context_session=self.session_id,
        )

        conn = _get_conn()
        try:
            table_names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertNotIn("user_facts", table_names)
            self.assertNotIn("self_facts", table_names)
        finally:
            conn.close()

        raw = """{
          "owner_updates": {
            "称呼偏好": "用户习惯称仆仆为姐姐"
          },
          "owner_delete_keys": ["nickname_for_pupu"],
          "instance_updates": {
            "自称": "姐姐"
          },
          "instance_delete_keys": ["仆仆自称"],
          "notes": "合并重复称呼事实"
        }"""

        with patch("pupu.maintenance.model_call.json_task", return_value=raw) as mock_json:
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        owner_facts = get_person_fact_map(owner_key)
        instance_facts = get_person_fact_map(INSTANCE_PERSON_KEY)

        self.assertEqual(mock_json.call_args.kwargs["task_name"], "facts_maintenance")
        self.assertEqual(owner_facts["称呼偏好"], "用户习惯称仆仆为姐姐")
        self.assertNotIn("nickname_for_pupu", owner_facts)
        self.assertEqual(owner_facts["身份"], "读研学生")
        self.assertEqual(instance_facts["自称"], "姐姐")
        self.assertNotIn("仆仆自称", instance_facts)
        self.assertEqual(instance_facts["喜欢的游戏"], "喜欢独立游戏")
        self.assertIn("- 模型归档事实：2", report)
        self.assertIn("- 模型更新事实：1", report)

    def test_fact_archive_is_soft_and_preserves_evidence(self):
        subject_key = person_from_session(self.session_id)
        saved = upsert_person_facts(
            {
                "临时状态": "今天正在下载游戏",
            },
            default_subject_person_key=subject_key,
            context_session=self.session_id,
            source_msg_start_id=10,
            source_msg_end_id=12,
        )
        fact_id = int(saved[0]["id"])
        raw = f"""{{
          "actions": [
            {{
              "action": "archive",
              "fact_id": {fact_id},
              "confidence": 0.99,
              "evidence_fact_ids": [{fact_id}],
              "reason": "这是一次性状态，不是稳定事实"
            }}
          ],
          "notes": "归档临时状态"
        }}"""

        with patch("pupu.maintenance.model_call.json_task", return_value=raw):
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM person_facts WHERE id = ?",
                (fact_id,),
            ).fetchone()
            evidence_count = conn.execute(
                "SELECT COUNT(*) AS c FROM fact_evidence WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()["c"]
            mutation = conn.execute(
                """SELECT action, status FROM memory_mutations
                   WHERE entity_type = 'person_fact' AND entity_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (str(fact_id),),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["status"], "archived")
        self.assertGreaterEqual(evidence_count, 1)
        self.assertEqual(dict(mutation), {"action": "archive", "status": "applied"})
        self.assertNotIn("临时状态", get_person_fact_map(subject_key))
        self.assertIn("- 模型归档事实：1", report)

    def test_auto_archive_applies_in_single_run(self):
        subject_key = person_from_session(self.session_id)
        saved = upsert_person_facts(
            {"临时状态": "今晚正在看电影"},
            default_subject_person_key=subject_key,
            context_session=self.session_id,
        )
        fact_id = int(saved[0]["id"])
        raw = f"""{{
          "actions": [{{
            "action": "archive",
            "fact_id": {fact_id},
            "confidence": 0.99,
            "evidence_fact_ids": [{fact_id}],
            "reason": "一次性状态"
          }}]
        }}"""

        with patch("pupu.maintenance.model_call.json_task", return_value=raw):
            report = run_memory_maintenance(
                trigger="auto",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        conn = _get_conn()
        try:
            mutation_statuses = [
                row["status"]
                for row in conn.execute(
                    """SELECT status FROM memory_mutations
                       WHERE entity_type = 'person_fact' AND entity_id = ?""",
                    (str(fact_id),),
                ).fetchall()
            ]
        finally:
            conn.close()

        self.assertIn("- 模型归档事实：1", report)
        self.assertNotIn("临时状态", get_person_fact_map(subject_key))
        self.assertEqual(mutation_statuses, ["applied"])

    def test_fact_merge_preserves_all_evidence(self):
        subject_key = person_from_session(self.session_id)
        first = upsert_person_facts(
            {"称呼偏好": "用户称仆仆为姐姐"},
            default_subject_person_key=subject_key,
            context_session=self.session_id,
            source_msg_start_id=10,
            source_msg_end_id=12,
        )[0]
        second = upsert_person_facts(
            {"对仆仆的称呼": "习惯叫仆仆姐姐"},
            default_subject_person_key=subject_key,
            context_session=self.session_id,
            source_msg_start_id=20,
            source_msg_end_id=22,
        )[0]
        target_id = int(first["id"])
        source_id = int(second["id"])
        raw = f"""{{
          "actions": [{{
            "action": "merge",
            "target_fact_id": {target_id},
            "source_fact_ids": [{source_id}],
            "value": "用户习惯称仆仆为姐姐",
            "confidence": 0.98,
            "evidence_fact_ids": [{target_id}, {source_id}],
            "reason": "两条事实表达同一稳定称呼偏好"
          }}]
        }}"""

        with patch("pupu.maintenance.model_call.json_task", return_value=raw):
            run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        conn = _get_conn()
        try:
            target = conn.execute(
                "SELECT fact_value, status FROM person_facts WHERE id = ?",
                (target_id,),
            ).fetchone()
            source = conn.execute(
                """SELECT status, superseded_by_id FROM person_facts
                   WHERE id = ?""",
                (source_id,),
            ).fetchone()
            evidence_count = conn.execute(
                "SELECT COUNT(*) AS c FROM fact_evidence WHERE fact_id = ?",
                (target_id,),
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(target["fact_value"], "用户习惯称仆仆为姐姐")
        self.assertEqual(target["status"], "active")
        self.assertEqual(source["status"], "superseded")
        self.assertEqual(source["superseded_by_id"], target_id)
        self.assertGreaterEqual(evidence_count, 2)

    def test_fact_plan_does_not_overwrite_concurrent_update(self):
        subject_key = person_from_session(self.session_id)
        fact = upsert_person_facts(
            {"所在地": "杭州"},
            default_subject_person_key=subject_key,
            context_session=self.session_id,
        )[0]
        fact_id = int(fact["id"])

        def fake_json_task(**kwargs):
            if kwargs["task_name"] == "facts_maintenance":
                update_person_fact_by_id(
                    fact_id,
                    value="上海",
                    confidence=1.0,
                    context_session=self.session_id,
                )
                return f"""{{
                  "actions": [{{
                    "action": "rewrite",
                    "fact_id": {fact_id},
                    "value": "杭州",
                    "confidence": 0.99,
                    "evidence_fact_ids": [{fact_id}],
                    "reason": "压缩表达"
                  }}]
                }}"""
            return '{"updates":[],"notes":""}'

        with patch(
            "pupu.maintenance.model_call.json_task",
            side_effect=fake_json_task,
        ):
            run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        self.assertEqual(get_person_fact_map(subject_key)["所在地"], "上海")

    def test_relationship_fact_is_included_in_maintenance_payload(self):
        upsert_person_facts(
            [
                {
                    "subject": "owner",
                    "object": "qq:123",
                    "scope": "relationship",
                    "key": "互动方式",
                    "value": "经常一起讨论游戏",
                }
            ],
            context_session=self.session_id,
        )
        payloads = []

        def fake_json_task(**kwargs):
            if kwargs["task_name"] == "facts_maintenance":
                payloads.append(kwargs["user_content"])
                return '{"actions":[],"notes":""}'
            return '{"updates":[],"notes":""}'

        with patch(
            "pupu.maintenance.model_call.json_task",
            side_effect=fake_json_task,
        ):
            run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
                semantic_mode="check",
            )

        self.assertTrue(payloads)
        self.assertIn('"scope": "relationship"', payloads[0])
        self.assertIn('"object_person_key": "qq:123"', payloads[0])

    def test_event_rollup_preserves_all_source_steps(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "long-project",
                    "title": "长期项目",
                    "kind": "project",
                    "details": "项目启动",
                    "confidence": 0.9,
                }
            ],
        )
        for index in range(13):
            append_event_step(
                self.session_id,
                "long-project",
                step_type="user",
                summary=f"项目进展 {index}",
            )
        _thread, before_steps = get_event_thread_steps(
            self.session_id,
            "long-project",
        )
        expected_through = int(before_steps[-5]["id"])

        def fake_json_task(**kwargs):
            if kwargs["task_name"] == "event_thread_rollup_maintenance":
                return f"""{{
                  "through_step_id": {expected_through},
                  "summary": "项目从启动推进到第八阶段，关键决策均已记录。",
                  "open_loops": "继续完成剩余阶段",
                  "stable_outcomes": "项目方向已经确定",
                  "confidence": 0.96,
                  "notes": "压缩旧步骤"
                }}"""
            return '{"updates":[],"notes":""}'

        with patch(
            "pupu.maintenance.model_call.json_task",
            side_effect=fake_json_task,
        ):
            report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        thread, after_steps = get_event_thread_steps(
            self.session_id,
            "long-project",
        )
        conn = _get_conn()
        try:
            rollup_count = conn.execute(
                """SELECT COUNT(*) AS c FROM event_thread_rollups
                   WHERE thread_id = ?""",
                (int(thread["id"]),),
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(len(after_steps), len(before_steps))
        self.assertEqual(thread["compacted_through_step_id"], expected_through)
        self.assertIn("项目从启动推进", thread["rollup_summary"])
        self.assertEqual(rollup_count, 1)
        self.assertIn("- 模型生成事件 checkpoint：1", report)

        with patch(
            "pupu.maintenance.model_call.json_task",
            side_effect=fake_json_task,
        ):
            second_report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 27, 3, 0, 0),
            )
        self.assertIn("- 模型生成事件 checkpoint：0", second_report)

    def test_auto_event_merge_applies_in_single_run_and_redirects_old_key(self):
        rows = upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "movie-plan",
                    "title": "五一一起看电影",
                    "kind": "promise",
                    "event_time": "2026-05-01",
                    "details": "约好五一看电影",
                    "confidence": 0.9,
                },
                {
                    "thread_key": "may-movie",
                    "title": "五一看电影计划",
                    "kind": "promise",
                    "event_time": "2026-05-01",
                    "details": "五一要一起看电影",
                    "confidence": 0.85,
                },
            ],
        )
        by_key = {row["thread_key"]: row for row in rows}
        canonical_id = int(by_key["movie-plan"]["id"])
        source_id = int(by_key["may-movie"]["id"])

        def fake_json_task(**kwargs):
            if kwargs["task_name"] == "event_thread_merge_maintenance":
                return f"""{{
                  "merges": [{{
                    "canonical_id": {canonical_id},
                    "source_ids": [{source_id}],
                    "title": "五一一起看电影",
                    "details": "用户和仆仆约定五一一起看电影。",
                    "followup_hint": "临近五一时自然确认片单",
                    "confidence": 0.98,
                    "reason": "人物、时间和事项完全一致"
                  }}],
                  "notes": "合并重复约定"
                }}"""
            return '{"updates":[],"notes":""}'

        with patch(
            "pupu.maintenance.model_call.json_task",
            side_effect=fake_json_task,
        ):
            report = run_memory_maintenance(
                trigger="auto",
                include_model=True,
                now=datetime(2026, 4, 26, 3, 0, 0),
            )

        redirected = get_event_thread_by_key(self.session_id, "may-movie")
        append_event_step(
            self.session_id,
            "may-movie",
            step_type="user",
            summary="用户补充了想看的片单",
        )
        _canonical, steps = get_event_thread_steps(self.session_id, "movie-plan")
        conn = _get_conn()
        try:
            source = conn.execute(
                """SELECT status, merged_into_thread_id FROM event_threads
                   WHERE id = ?""",
                (source_id,),
            ).fetchone()
            mutation_count = conn.execute(
                """SELECT COUNT(*) AS c FROM memory_mutations
                   WHERE entity_type = 'event_thread' AND action = 'merge'
                     AND status = 'applied'"""
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["merged_into_thread_id"], canonical_id)
        self.assertEqual(int(redirected["id"]), canonical_id)
        self.assertGreaterEqual(len(steps), 4)
        self.assertEqual(steps[-1]["summary"], "用户补充了想看的片单")
        self.assertEqual(mutation_count, 1)
        self.assertIn("- 模型合并事件线：1", report)

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

        with patch("pupu.maintenance.model_call.json_task", return_value=raw):
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

        with patch("pupu.maintenance.model_call.json_task", return_value=raw):
            second_report = run_memory_maintenance(
                trigger="manual",
                include_model=True,
                now=datetime(2026, 4, 27, 3, 0, 0),
            )
        _kept, second_steps = get_event_thread_steps(
            self.session_id,
            "movie-plan",
        )
        self.assertEqual(len(second_steps), len(kept_steps))
        self.assertIn("- 模型更新事件线：0", second_report)


if __name__ == "__main__":
    unittest.main()
