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
    get_event_thread_recent_steps,
    get_event_thread_steps,
    get_important_events,
    _get_conn,
    init_db,
    link_important_event_task,
    reset_session,
    save_message_with_speaker,
    upsert_important_events,
)
from pupu.storage.people import qq_person_key, upsert_person
import pupu.storage.important_events as important_event_store
from pupu.storage.db import get_conn
from pupu.storage.scheduled_tasks import cancel_scheduled_task


class EventGraphMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_event_graph_{self._testMethodName}"
        reset_session(self.session_id)
        conn = get_conn()
        try:
            conn.execute("DELETE FROM people")
            conn.commit()
        finally:
            conn.close()

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

    def test_event_thread_fts_index_is_created_and_refreshed(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "fts-cake",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让仆仆验收",
                    "confidence": 0.9,
                }
            ],
        )
        append_event_step(
            self.session_id,
            "fts-cake",
            step_type="user",
            summary="用户补充说要检查草莓蛋糕上的大颗草莓",
        )

        conn = _get_conn()
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'event_thread_fts'"
            ).fetchone()
            fts_row = conn.execute(
                """SELECT search_text
                   FROM event_thread_fts
                   WHERE session_id = ? AND search_text MATCH ?""",
                (self.session_id, '"大颗草莓"'),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(table)
        self.assertIsNotNone(fts_row)

    def test_related_search_marks_fts_candidate_in_debug(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "debug-cake",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让仆仆验收",
                    "merge_hint": "草莓蛋糕 验收 大颗草莓",
                    "confidence": 0.95,
                }
            ],
        )

        matches = find_related_event_threads(
            self.session_id,
            "今天要检查草莓蛋糕",
            limit=3,
            debug=True,
        )

        self.assertEqual(matches[0]["source_event_key"], "debug-cake")
        self.assertTrue(matches[0]["match_debug"]["fts_attempted"])
        self.assertTrue(matches[0]["match_debug"]["used_fts_candidate"])
        self.assertGreater(matches[0]["match_debug"]["fts_score"], 0)

    def test_related_search_falls_back_when_fts_unavailable(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "fallback-cake",
                    "title": "草莓蛋糕验收",
                    "kind": "promise",
                    "details": "用户答应带草莓蛋糕让仆仆验收",
                    "merge_hint": "草莓 蛋糕 验收",
                    "confidence": 0.95,
                }
            ],
        )

        original = important_event_store._event_thread_fts_available
        important_event_store._event_thread_fts_available = lambda conn: False
        try:
            matches = find_related_event_threads(
                self.session_id,
                "今天要检查草莓蛋糕",
                limit=3,
                debug=True,
            )
        finally:
            important_event_store._event_thread_fts_available = original

        self.assertEqual(matches[0]["source_event_key"], "fallback-cake")
        self.assertFalse(matches[0]["match_debug"]["fts_attempted"])
        self.assertFalse(matches[0]["match_debug"]["used_fts_candidate"])

    def test_recent_steps_returns_last_steps_in_chronological_order(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "steps-demo",
                    "title": "事件线步骤测试",
                    "kind": "promise",
                    "details": "第一步",
                    "confidence": 0.8,
                }
            ],
        )
        append_event_step(self.session_id, "steps-demo", step_type="user", summary="第二步")
        append_event_step(self.session_id, "steps-demo", step_type="instance", summary="第三步")

        steps = get_event_thread_recent_steps(self.session_id, "steps-demo", limit=2)

        self.assertEqual([step["summary"] for step in steps], ["第二步", "第三步"])

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
        step_edges = [edge for edge in payload["edges"] if edge.get("type") != "person_thread"]
        person_edges = [edge for edge in payload["edges"] if edge.get("type") == "person_thread"]
        self.assertEqual(len(step_edges), 2)
        self.assertTrue(person_edges)
        self.assertEqual(len({edge["id"] for edge in payload["edges"]}), len(payload["edges"]))
        self.assertEqual(
            len({(edge["source"], edge["target"]) for edge in person_edges}),
            len(person_edges),
        )

    def test_event_threads_have_default_people(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "people-default",
                    "title": "People Default",
                    "kind": "milestone",
                    "details": "User and instance share this event.",
                    "confidence": 1.0,
                }
            ],
        )

        event, steps = get_event_thread_steps(self.session_id, "people-default")
        payload = event_graph_payload(self.session_id)
        person_keys = {person["person_key"] for person in event["people"]}

        self.assertIn("owner", person_keys)
        self.assertIn("instance", person_keys)
        self.assertIn("用户", event["people_label"])
        self.assertTrue(steps[0]["people"])
        self.assertTrue(any(node["type"] == "person" for node in payload["nodes"]))

    def test_event_people_are_inferred_from_message_range(self):
        speaker_key = qq_person_key("123456")
        start_id = save_message_with_speaker(
            "user",
            "Alice wants to check the cake.",
            self.session_id,
            speaker_key=speaker_key,
            speaker_name="Alice",
            speaker_qq="123456",
        )
        end_id = save_message_with_speaker(
            "assistant",
            "Instance agrees to remember it.",
            self.session_id,
            speaker_key="instance",
            speaker_name="Lulu",
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "alice-cake",
                    "title": "Alice cake check",
                    "kind": "promise",
                    "details": "Alice and Lulu agreed to check the cake.",
                    "source_context_session": self.session_id,
                    "source_msg_start_id": start_id,
                    "source_msg_end_id": end_id,
                    "confidence": 0.9,
                }
            ],
        )

        event, steps = get_event_thread_steps(self.session_id, "alice-cake")
        person_keys = {person["person_key"] for person in event["people"]}

        self.assertIn(speaker_key, person_keys)
        self.assertIn("instance", person_keys)
        self.assertIn("Alice", event["people_label"])
        self.assertIn(speaker_key, {person["person_key"] for person in steps[0]["people"]})

    def test_qq_person_display_name_is_fixed_and_later_names_become_aliases(self):
        speaker_key = qq_person_key("123456")
        first_id = save_message_with_speaker(
            "user",
            "Alice starts a cake thread.",
            self.session_id,
            speaker_key=speaker_key,
            speaker_name="Alice",
            speaker_qq="123456",
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "fixed-alice",
                    "title": "Alice fixed name",
                    "kind": "promise",
                    "details": "Alice starts a cake thread.",
                    "source_context_session": self.session_id,
                    "source_msg_start_id": first_id,
                    "source_msg_end_id": first_id,
                    "confidence": 0.9,
                }
            ],
        )
        second_id = save_message_with_speaker(
            "user",
            "A changed group card continues the same cake thread.",
            self.session_id,
            speaker_key=speaker_key,
            speaker_name="Cake Captain",
            speaker_qq="123456",
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "action": "append_step",
                    "source_event_key": "fixed-alice",
                    "summary": "The same QQ account continues the cake thread.",
                    "source_context_session": self.session_id,
                    "source_msg_start_id": second_id,
                    "source_msg_end_id": second_id,
                }
            ],
        )

        event, _steps = get_event_thread_steps(self.session_id, "fixed-alice")
        conn = get_conn()
        try:
            person = conn.execute(
                "SELECT display_name, aliases FROM people WHERE person_key = ?",
                (speaker_key,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(person)
        self.assertEqual(person["display_name"], "Alice")
        self.assertIn("Alice", event["people_label"])
        self.assertNotIn("Cake Captain /", event["people_label"])
        self.assertIn("Cake Captain", person["aliases"])

    def test_owner_display_name_is_fixed_and_later_qq_names_become_aliases(self):
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "owner",
                kind="owner",
                display_name="小夫",
                qq_id="424225912",
                aliases=["用户"],
            )
            conn.commit()
        finally:
            conn.close()

        first_id = save_message_with_speaker(
            "user",
            "Owner starts a cake thread.",
            self.session_id,
            speaker_key="owner",
            speaker_name="群昵称会变",
            speaker_qq="424225912",
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "fixed-owner",
                    "title": "Owner fixed name",
                    "kind": "promise",
                    "details": "Owner starts a cake thread.",
                    "source_context_session": self.session_id,
                    "source_msg_start_id": first_id,
                    "source_msg_end_id": first_id,
                    "confidence": 0.9,
                }
            ],
        )

        event, _steps = get_event_thread_steps(self.session_id, "fixed-owner")
        conn = get_conn()
        try:
            person = conn.execute(
                "SELECT kind, display_name, qq_id, aliases FROM people WHERE person_key = 'owner'",
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(person)
        self.assertEqual(person["kind"], "owner")
        self.assertEqual(person["display_name"], "小夫")
        self.assertEqual(person["qq_id"], "424225912")
        self.assertIn("小夫", event["people_label"])
        self.assertNotIn("群昵称会变 /", event["people_label"])
        self.assertIn("群昵称会变", person["aliases"])

    def test_related_search_boosts_matching_people(self):
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "owner-cake",
                    "title": "Cake check",
                    "kind": "promise",
                    "details": "Owner agreed to check the strawberry cake.",
                    "merge_hint": "cake strawberry check",
                    "confidence": 0.9,
                }
            ],
        )
        speaker_key = qq_person_key("9988")
        msg_id = save_message_with_speaker(
            "user",
            "Friend also agreed to check the strawberry cake.",
            self.session_id,
            speaker_key=speaker_key,
            speaker_name="Friend",
            speaker_qq="9988",
        )
        upsert_important_events(
            self.session_id,
            [
                {
                    "source_event_key": "friend-cake",
                    "title": "Cake check",
                    "kind": "promise",
                    "details": "Friend agreed to check the strawberry cake.",
                    "merge_hint": "cake strawberry check",
                    "source_context_session": self.session_id,
                    "source_msg_start_id": msg_id,
                    "source_msg_end_id": msg_id,
                    "confidence": 0.9,
                }
            ],
        )

        matches = find_related_event_threads(
            self.session_id,
            "check strawberry cake",
            limit=2,
            person_keys={speaker_key},
            debug=True,
        )

        self.assertEqual(matches[0]["source_event_key"], "friend-cake")
        self.assertGreater(matches[0]["match_debug"]["people_bonus"], 0)


if __name__ == "__main__":
    unittest.main()
