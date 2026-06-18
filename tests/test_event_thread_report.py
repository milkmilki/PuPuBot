import os
from datetime import datetime, timedelta
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.event_thread_report import format_event_threads_report
from pupu.memory import _get_conn, init_db, reset_session, upsert_event_threads


class EventThreadReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_event_thread_report_{self._testMethodName}"
        reset_session(self.session_id)

    def test_report_includes_confidence_and_key(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "birthday-2026-04-27",
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

        report = format_event_threads_report(self.session_id)

        self.assertIn("事件线 1 条", report)
        self.assertIn("confidence=0.95", report)
        self.assertIn("key=birthday-2026-04-27", report)
        self.assertIn("用户明天生日", report)

    def test_report_includes_all_events_with_newest_first(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "old-event",
                    "title": "Old event",
                    "kind": "promise",
                    "time_text": "old",
                    "details": "older",
                    "confidence": 0.8,
                },
                {
                    "thread_key": "new-event",
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

        report = format_event_threads_report(self.session_id)

        self.assertIn("事件线 2 条", report)
        self.assertIn("Old event", report)
        self.assertIn("New event", report)
        self.assertLess(report.index("New event"), report.index("Old event"))

    def test_report_does_not_show_memu_sync_status(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "event-a",
                    "title": "Event A",
                    "kind": "milestone",
                    "details": "A",
                    "confidence": 1.0,
                }
            ],
        )

        report = format_event_threads_report(self.session_id)

        self.assertNotIn("memU", report)
        self.assertIn("Event A", report)

    def test_report_detail_and_search_use_event_threads(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "camping-plan",
                    "title": "一起看摇曳露营",
                    "kind": "promise",
                    "details": "用户和璐璐约定晚上一起看摇曳露营第一集",
                    "followup_hint": "晚上自然问用户是否开始看",
                    "confidence": 0.9,
                }
            ],
        )
        upsert_event_threads(
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

        detail = format_event_threads_report(self.session_id, query="detail camping-plan")
        search = format_event_threads_report(self.session_id, query="search 洗澡 露营")

        self.assertIn("事件线：一起看摇曳露营", detail)
        self.assertIn("进展 2 条", detail)
        self.assertIn("用户补充了开始前的安排", detail)
        self.assertIn("相关事件线 1 条", search)
        self.assertIn("camping-plan", search)

    def test_report_url_generates_standalone_graph_html(self):
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "graph-plan",
                    "title": "Graph Plan",
                    "kind": "promise",
                    "details": "User and instance agreed to inspect the event graph.",
                    "confidence": 0.9,
                }
            ],
        )

        report = format_event_threads_report(self.session_id, query="url")

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
        upsert_event_threads(
            self.session_id,
            [
                {
                    "thread_key": "debug-search",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让璐璐验收",
                    "merge_hint": "草莓蛋糕 验收 大颗草莓",
                    "confidence": 0.95,
                }
            ],
        )

        report = format_event_threads_report(
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
