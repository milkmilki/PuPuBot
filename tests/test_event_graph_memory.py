import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_MEMU_ENABLED"] = "false"

from pupu.memory import (
    append_event_step,
    create_scheduled_task,
    event_graph_payload,
    find_related_event_threads,
    get_event_thread_steps,
    get_important_events,
    init_db,
    link_important_event_task,
    reset_session,
    upsert_important_events,
)
from pupu.storage.scheduled_tasks import cancel_scheduled_task


class EventGraphMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_event_graph_{self._testMethodName}"
        reset_session(self.session_id)

    def test_legacy_upsert_creates_thread_and_step(self):
        rows = upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "first-camp",
                    "title": "第一次露营约定",
                    "kind": "promise",
                    "details": "用户和仆仆约定周末一起看露营攻略",
                    "followup_hint": "周末提醒用户看攻略",
                    "confidence": 0.85,
                }
            ],
        )

        event, steps = get_event_thread_steps(self.session_id, "first-camp")

        self.assertEqual(rows[0]["source_event_key"], "first-camp")
        self.assertEqual(event["title"], "第一次露营约定")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_type"], "user")
        self.assertIn("看露营攻略", steps[0]["summary"])

    def test_similar_event_without_key_merges_into_existing_thread(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "camp-plan",
                    "title": "露营动画计划",
                    "kind": "promise",
                    "details": "用户和仆仆约定晚上一起看摇曳露营",
                    "confidence": 0.9,
                }
            ],
        )

        rows = upsert_important_events(
            self.session_id,
            [
                {
                    "title": "继续露营动画计划",
                    "kind": "promise",
                    "details": "用户说洗澡后继续一起看摇曳露营",
                    "confidence": 0.8,
                }
            ],
        )
        events = get_important_events(self.session_id, limit=5)
        _event, steps = get_event_thread_steps(self.session_id, "camp-plan")

        self.assertEqual(len(events), 1)
        self.assertEqual(rows[0]["source_event_key"], "camp-plan")
        self.assertEqual(len(steps), 2)
        self.assertIn("洗澡后继续", steps[-1]["summary"])

    def test_time_step_is_marked_as_inferred(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "commute",
                    "title": "用户通勤状态",
                    "kind": "state",
                    "details": "用户正在坐地铁回家",
                    "confidence": 0.7,
                }
            ],
        )
        append_event_step(
            self.session_id,
            "commute",
            step_type="time",
            summary="用户已经到家",
            cause="距离坐地铁已经过去两个小时",
        )

        _event, steps = get_event_thread_steps(self.session_id, "commute")

        self.assertEqual(steps[-1]["step_type"], "time")
        self.assertTrue(steps[-1]["summary"].startswith("推测："))

    def test_related_search_uses_current_step_and_merge_hint(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "cake-check",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让仆仆验收",
                    "followup_hint": "看到蛋糕时自然验收草莓大小",
                    "merge_hint": "草莓 蛋糕 验收",
                    "confidence": 0.95,
                }
            ],
        )

        matches = find_related_event_threads(self.session_id, "今天要检查草莓蛋糕", limit=3)

        self.assertEqual(matches[0]["source_event_key"], "cake-check")
        self.assertGreater(matches[0]["score"], 0)

    def test_scheduled_task_cancel_updates_thread_with_system_step(self):
        task_id = create_scheduled_task(
            self.session_id,
            "生日提醒",
            "祝用户生日快乐",
            "2026-04-27T09:00:00",
            "once",
            None,
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "birthday",
                    "title": "用户生日",
                    "kind": "birthday",
                    "details": "用户生日需要祝福",
                    "confidence": 1.0,
                }
            ],
        )
        link_important_event_task(self.session_id, "birthday", task_id)

        self.assertTrue(cancel_scheduled_task(self.session_id, task_id))
        event, steps = get_event_thread_steps(self.session_id, "birthday")

        self.assertEqual(event["status"], "cancelled")
        self.assertIsNone(event["linked_task_id"])
        self.assertEqual(steps[-1]["step_type"], "system")
        self.assertIn("取消", steps[-1]["summary"] + steps[-1]["cause"])

    def test_event_graph_payload_contains_nodes_and_edges(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "graph-demo",
                    "title": "图谱测试",
                    "kind": "milestone",
                    "details": "第一步",
                    "confidence": 1.0,
                }
            ],
        )
        append_event_step(
            self.session_id,
            "graph-demo",
            step_type="instance",
            summary="第二步",
            cause="仆仆推动事件进展",
            reflection="这次推动让对话更自然",
        )

        payload = event_graph_payload(self.session_id)

        self.assertEqual(len(payload["threads"]), 1)
        self.assertEqual(len(payload["steps"]), 2)
        self.assertGreaterEqual(len(payload["nodes"]), 3)
        self.assertEqual(len(payload["edges"]), 2)


if __name__ == "__main__":
    unittest.main()
