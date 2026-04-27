import os
from datetime import datetime
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.memory import (
    create_scheduled_task,
    get_important_events,
    init_db,
    list_scheduled_tasks,
    reset_session,
)
from pupu.review_followups import (
    apply_review_task_drafts,
    apply_review_task_updates,
    normalize_review_important_events,
    normalize_review_task_drafts,
    normalize_review_task_updates,
    save_review_important_events,
)


class ReviewFollowupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = "test_review_followups"
        reset_session(self.session_id)

    def test_birthday_date_only_task_draft_creates_task_and_links_event(self):
        important_events = normalize_review_important_events(
            [
                {
                    "source_event_key": "birthday-2026-04-27",
                    "title": "user birthday tomorrow",
                    "kind": "birthday",
                    "event_time": "2026-04-27",
                    "time_text": "tomorrow",
                    "details": "user said birthday tomorrow",
                    "followup_hint": "wish happy birthday",
                    "confidence": 0.95,
                }
            ]
        )
        saved = save_review_important_events(self.session_id, important_events)
        task_drafts = normalize_review_task_drafts(
            [
                {
                    "source_event_key": "birthday-2026-04-27",
                    "should_create": True,
                    "title": "birthday wish",
                    "instruction": "wish happy birthday",
                    "run_at": "2026-04-27",
                    "repeat": "once",
                    "kind": "birthday",
                }
            ]
        )

        results = apply_review_task_drafts(
            self.session_id,
            task_drafts,
            saved,
            now=datetime(2026, 4, 26, 20, 0, 0),
        )

        self.assertEqual(results[0]["status"], "created")
        tasks = list_scheduled_tasks(self.session_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["run_at"], "2026-04-27T09:00:00")

        events = get_important_events(self.session_id, limit=5)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["linked_task_id"], results[0]["task_id"])
        self.assertEqual(events[0]["status"], "scheduled")

    def test_duplicate_task_draft_links_existing_task_instead_of_creating_second_one(self):
        important_events = normalize_review_important_events(
            [
                {
                    "source_event_key": "birthday-2026-04-27",
                    "title": "user birthday tomorrow",
                    "kind": "birthday",
                    "event_time": "2026-04-27",
                    "time_text": "tomorrow",
                    "details": "user said birthday tomorrow",
                    "followup_hint": "wish happy birthday",
                    "confidence": 0.95,
                }
            ]
        )
        saved = save_review_important_events(self.session_id, important_events)
        task_drafts = normalize_review_task_drafts(
            [
                {
                    "source_event_key": "birthday-2026-04-27",
                    "should_create": True,
                    "title": "birthday wish",
                    "instruction": "wish happy birthday",
                    "run_at": "2026-04-27",
                    "repeat": "once",
                    "kind": "birthday",
                }
            ]
        )

        first = apply_review_task_drafts(
            self.session_id,
            task_drafts,
            saved,
            now=datetime(2026, 4, 26, 20, 0, 0),
        )
        second = apply_review_task_drafts(
            self.session_id,
            task_drafts,
            saved,
            now=datetime(2026, 4, 26, 20, 1, 0),
        )

        self.assertEqual(first[0]["status"], "created")
        self.assertEqual(second[0]["status"], "linked_existing")
        self.assertEqual(len(list_scheduled_tasks(self.session_id)), 1)

    def test_task_update_cancel_matching_cancels_existing_task(self):
        task_id = create_scheduled_task(
            self.session_id,
            "睡觉提醒",
            "提醒用户睡觉",
            "2026-04-26T23:00:00",
            "once",
            None,
        )
        updates = normalize_review_task_updates(
            [
                {
                    "action": "cancel_matching",
                    "query": "睡觉提醒",
                    "reason": "用户已经准备睡觉",
                }
            ]
        )

        results = apply_review_task_updates(self.session_id, updates)

        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(results[0]["task_ids"], [task_id])
        self.assertEqual(list_scheduled_tasks(self.session_id), [])

    def test_task_update_create_creates_task(self):
        updates = normalize_review_task_updates(
            [
                {
                    "action": "create",
                    "source_event_key": "wake-up",
                    "title": "早起提醒",
                    "instruction": "提醒用户起床",
                    "run_at": "2026-04-27T06:00:00",
                    "repeat": "once",
                    "reason": "用户说想 6 点早起",
                }
            ]
        )

        results = apply_review_task_updates(
            self.session_id,
            updates,
            now=datetime(2026, 4, 26, 20, 0, 0),
        )

        tasks = list_scheduled_tasks(self.session_id)
        self.assertEqual(results[0]["action"], "create")
        self.assertEqual(results[0]["status"], "created")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["run_at"], "2026-04-27T06:00:00")

    def test_task_update_reschedule_matching_updates_existing_task(self):
        task_id = create_scheduled_task(
            self.session_id,
            "早起提醒",
            "提醒用户起床",
            "2026-04-27T06:00:00",
            "once",
            None,
        )
        updates = normalize_review_task_updates(
            [
                {
                    "action": "reschedule_matching",
                    "query": "早起提醒",
                    "run_at": "2026-04-27T09:00:00",
                    "reason": "用户后来改成 9 点早起",
                }
            ]
        )

        results = apply_review_task_updates(
            self.session_id,
            updates,
            now=datetime(2026, 4, 26, 20, 0, 0),
        )

        tasks = list_scheduled_tasks(self.session_id)
        self.assertEqual(results[0]["status"], "rescheduled")
        self.assertEqual(results[0]["task_ids"], [task_id])
        self.assertEqual(tasks[0]["run_at"], "2026-04-27T09:00:00")


if __name__ == "__main__":
    unittest.main()
