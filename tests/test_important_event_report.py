import os
import json
from pathlib import Path
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.important_event_report import format_important_events_report
from pupu.memory import _get_conn, init_db, reset_session, upsert_important_events


class ImportantEventReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_important_event_report_{self._testMethodName}"
        reset_session(self.session_id)

    def test_report_includes_confidence_and_key(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "birthday-2026-04-27",
                    "title": "用户明天生日",
                    "kind": "birthday",
                    "event_time": "2026-04-27",
                    "time_text": "明天",
                    "details": "用户说明天过生日",
                    "followup_hint": "记得先祝她生日快乐",
                    "confidence": 0.95,
                    "status": "active",
                }
            ],
        )

        report = format_important_events_report(self.session_id)

        self.assertIn("事件线 1 条", report)
        self.assertIn("confidence=0.95", report)
        self.assertIn("key=birthday-2026-04-27", report)
        self.assertIn("用户明天生日", report)

    def test_report_includes_all_events_with_newest_first(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "old-event",
                    "title": "Old event",
                    "kind": "promise",
                    "time_text": "old",
                    "details": "older",
                    "confidence": 0.8,
                },
                {
                    "source_event_key": "new-event",
                    "title": "New event",
                    "kind": "milestone",
                    "time_text": "new",
                    "details": "newer",
                    "confidence": 1.0,
                },
            ],
        )
        old_time = (datetime.now() - timedelta(days=1)).isoformat()
        new_time = datetime.now().isoformat()
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE event_threads SET updated_at = ?, created_at = ? "
                "WHERE session_id = ? AND key = ?",
                (old_time, old_time, self.session_id, "old-event"),
            )
            conn.execute(
                "UPDATE event_threads SET updated_at = ?, created_at = ? "
                "WHERE session_id = ? AND key = ?",
                (new_time, new_time, self.session_id, "new-event"),
            )
            conn.commit()
        finally:
            conn.close()

        report = format_important_events_report(self.session_id)

        self.assertIn("事件线 2 条", report)
        self.assertIn("Old event", report)
        self.assertIn("New event", report)
        self.assertLess(report.index("New event"), report.index("Old event"))

    def test_report_does_not_show_memu_sync_status(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "event-a",
                    "title": "Event A",
                    "kind": "milestone",
                    "details": "A",
                    "confidence": 1.0,
                }
            ],
        )

        report = format_important_events_report(self.session_id)

        self.assertNotIn("memU", report)
        self.assertIn("Event A", report)

    def test_report_detail_and_search_use_event_threads(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "camping-plan",
                    "title": "一起看摇曳露营",
                    "kind": "promise",
                    "details": "用户和仆仆约定晚上一起看摇曳露营第一集",
                    "followup_hint": "晚上自然问用户是否开始看",
                    "confidence": 0.9,
                }
            ],
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "action": "append_step",
                    "thread_key": "camping-plan",
                    "step_type": "user",
                    "summary": "用户说先洗澡，洗完再一起看摇曳露营",
                    "cause": "用户补充了开始前的安排",
                }
            ],
        )

        detail = format_important_events_report(self.session_id, query="detail camping-plan")
        search = format_important_events_report(self.session_id, query="search 洗澡 露营")

        self.assertIn("事件线：一起看摇曳露营", detail)
        self.assertIn("进展 2 条", detail)
        self.assertIn("用户补充了开始前的安排", detail)
        self.assertIn("相关事件线 1 条", search)
        self.assertIn("camping-plan", search)


    def _insert_legacy_event(
        self,
        *,
        key: str,
        title: str,
        details: str,
        kind: str = "promise",
        event_time: str = "2026-06-10",
        time_text: str = "tonight",
        followup_hint: str = "Ask whether the plan happened.",
        confidence: float = 0.88,
    ) -> None:
        now = datetime.now().isoformat()
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO important_events
                   (session_id, source_event_key, title, kind, event_time, time_text,
                    details, followup_hint, confidence, status, linked_task_id,
                    last_seen_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.session_id,
                    key,
                    title,
                    kind,
                    event_time,
                    time_text,
                    details,
                    followup_hint,
                    confidence,
                    "active",
                    None,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_report_simple_migrate_imports_legacy_important_events_once(self):
        self._insert_legacy_event(
            key="legacy-date",
            title="Legacy Date",
            details="Legacy row should become an event step.",
        )

        first_report = format_important_events_report(self.session_id, query="migrate simple")
        second_report = format_important_events_report(self.session_id, query="migrate simple")

        conn = _get_conn()
        try:
            thread_count = conn.execute(
                "SELECT COUNT(*) AS c FROM event_threads WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["c"]
            step_count = conn.execute(
                """SELECT COUNT(*) AS c
                   FROM event_steps
                   WHERE thread_id IN (
                       SELECT id FROM event_threads WHERE session_id = ?
                   )""",
                (self.session_id,),
            ).fetchone()["c"]
        finally:
            conn.close()

        self.assertIn("legacy=1", first_report)
        self.assertIn("created=1", first_report)
        self.assertIn("skipped=0", first_report)
        self.assertIn("failed=0", first_report)
        self.assertIn("created=0", second_report)
        self.assertIn("skipped=1", second_report)
        self.assertEqual(thread_count, 1)
        self.assertEqual(step_count, 1)

    def test_report_model_migrate_can_merge_legacy_events_into_steps(self):
        self._insert_legacy_event(
            key="camping-plan-a",
            title="Camping plan",
            details="User and instance agreed to watch a camping show.",
            followup_hint="Ask when the episode starts.",
        )
        self._insert_legacy_event(
            key="camping-plan-b",
            title="Camping plan update",
            details="User said to shower first, then watch the camping show.",
            followup_hint="Ask after shower.",
        )

        model_json = """
        {
          "threads": [
            {
              "key": "camping-plan",
              "title": "Camping plan",
              "kind": "promise",
              "status": "active",
              "event_time": "2026-06-10",
              "followup_hint": "Ask whether they are ready to watch.",
              "merge_hint": "camping show shower watch",
              "confidence": 0.9,
              "source_ids": [1, 2],
              "steps": [
                {
                  "step_type": "system",
                  "summary": "User and instance agreed to watch a camping show.",
                  "cause": "Merged from legacy event camping-plan-a.",
                  "occurred_at": "2026-06-10"
                },
                {
                  "step_type": "user",
                  "summary": "User said to shower first, then watch the camping show.",
                  "cause": "Merged from legacy event camping-plan-b.",
                  "occurred_at": "2026-06-10"
                }
              ]
            }
          ],
          "notes": "Merged two related legacy rows into one event line."
        }
        """
        with patch("pupu.important_event_report.json_task", return_value=model_json):
            report = format_important_events_report(self.session_id, query="migrate")

        event_report = format_important_events_report(self.session_id)
        detail = format_important_events_report(self.session_id, query="detail camping-plan")

        self.assertIn("事件图谱模型迁移完成", report)
        self.assertIn("legacy=2", report)
        self.assertIn("planned_threads=1", report)
        self.assertIn("created=1", report)
        self.assertIn("steps=2", report)
        self.assertIn("事件线 1 条", event_report)
        self.assertIn("进展 2 条", detail)
        self.assertIn("shower first", detail)

    def test_report_model_migrate_uses_bounded_token_budget_and_parses_thinking_blocks(self):
        self._insert_legacy_event(
            key="thinking-response",
            title="Thinking Response",
            details="A legacy row migrated from a response with thinking.",
        )

        model_json = """
        <think>
        I might mention {"not": "the final object"} while reasoning.
        </think>
        Here is the JSON:
        {
          "threads": [
            {
              "key": "thinking-response",
              "title": "Thinking Response",
              "kind": "milestone",
              "status": "active",
              "confidence": 0.9,
              "steps": [
                {
                  "step_type": "system",
                  "summary": "A legacy row migrated from a response with thinking.",
                  "cause": "Merged from legacy."
                }
              ]
            }
          ]
        }
        """
        captured = {}

        def fake_json_task(**kwargs):
            captured.update(kwargs)
            return model_json

        with patch("pupu.important_event_report.json_task", side_effect=fake_json_task):
            report = format_important_events_report(self.session_id, query="migrate")

        self.assertLessEqual(captured["max_tokens"], 20000)
        self.assertIn("max_tokens_per_batch=", report)
        self.assertIn("created=1", report)
        self.assertIn("steps=1", report)

    def test_report_model_migrate_batches_and_merges_existing_planned_threads(self):
        for index in range(4):
            self._insert_legacy_event(
                key=f"batch-{index}",
                title=f"Batch {index}",
                details=f"Batch detail {index}.",
            )

        responses = [
            """
            {
              "threads": [
                {
                  "key": "batched-plan",
                  "title": "Batched Plan",
                  "kind": "promise",
                  "status": "active",
                  "steps": [
                    {"step_type": "system", "summary": "Batch detail 0.", "cause": "Batch 1"},
                    {"step_type": "system", "summary": "Batch detail 1.", "cause": "Batch 1"}
                  ]
                }
              ]
            }
            """,
            """
            {
              "threads": [
                {
                  "key": "batched-plan",
                  "title": "Batched Plan",
                  "kind": "promise",
                  "status": "active",
                  "steps": [
                    {"step_type": "system", "summary": "Batch detail 2.", "cause": "Batch 2"},
                    {"step_type": "system", "summary": "Batch detail 3.", "cause": "Batch 2"}
                  ]
                }
              ]
            }
            """,
        ]
        calls = []

        def fake_json_task(**kwargs):
            calls.append(kwargs)
            return responses[len(calls) - 1]

        with patch.dict("os.environ", {"PUPU_EVENT_MIGRATION_BATCH_SIZE": "2"}, clear=False):
            with patch("pupu.important_event_report.json_task", side_effect=fake_json_task):
                report = format_important_events_report(self.session_id, query="migrate")

        detail = format_important_events_report(self.session_id, query="detail batched-plan")
        second_payload = json.loads(calls[1]["user_content"])

        self.assertEqual(len(calls), 2)
        self.assertIn("batches=2", report)
        self.assertIn("planned_threads=1", report)
        self.assertIn("steps=4", report)
        self.assertIn("进展 4 条", detail)
        self.assertEqual(second_payload["existing_threads"][0]["key"], "batched-plan")

    def test_report_model_migrate_replaces_prior_simple_migration_threads(self):
        self._insert_legacy_event(
            key="legacy-a",
            title="Legacy A",
            details="First simple migrated row.",
        )
        self._insert_legacy_event(
            key="legacy-b",
            title="Legacy B",
            details="Second simple migrated row.",
        )
        simple_report = format_important_events_report(self.session_id, query="migrate simple")
        self.assertIn("created=2", simple_report)

        model_json = """
        {
          "threads": [
            {
              "key": "merged-legacy",
              "title": "Merged Legacy",
              "kind": "milestone",
              "status": "active",
              "confidence": 0.9,
              "steps": [
                {"step_type": "system", "summary": "First simple migrated row.", "cause": "Merged"},
                {"step_type": "system", "summary": "Second simple migrated row.", "cause": "Merged"}
              ]
            }
          ]
        }
        """
        with patch("pupu.important_event_report.json_task", return_value=model_json):
            report = format_important_events_report(self.session_id, query="migrate")

        event_report = format_important_events_report(self.session_id)

        self.assertIn("removed_simple=2", report)
        self.assertIn("created=1", report)
        self.assertIn("steps=2", report)
        self.assertIn("事件线 1 条", event_report)
        self.assertIn("Merged Legacy", event_report)


    def test_report_url_generates_standalone_graph_html(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "graph-plan",
                    "title": "Graph Plan",
                    "kind": "promise",
                    "details": "User and instance agreed to inspect the event graph.",
                    "confidence": 0.9,
                }
            ],
        )

        report = format_important_events_report(self.session_id, query="url")

        self.assertIn("file:///", report)
        self.assertIn("event-graph-", report)
        path_line = next(line for line in report.splitlines() if line.startswith("本地路径："))
        html_path = Path(path_line.removeprefix("本地路径："))
        self.assertTrue(html_path.is_file())
        try:
            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("PuPu 事件图谱", html_text)
            self.assertIn("Graph Plan", html_text)
        finally:
            html_path.unlink(missing_ok=True)

    def test_search_debug_report_includes_score_breakdown(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "debug-search",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让仆仆验收",
                    "merge_hint": "草莓蛋糕 验收 大颗草莓",
                    "confidence": 0.95,
                }
            ],
        )

        report = format_important_events_report(
            self.session_id,
            query="search --debug 今天要检查草莓蛋糕",
        )

        self.assertIn("相关事件线 1 条（debug）", report)
        self.assertIn("debug-search", report)
        self.assertIn("fts=", report)
        self.assertIn("overlap=", report)
        self.assertIn("status_bonus=", report)
        self.assertIn("used_fts=True", report)


if __name__ == "__main__":
    unittest.main()
