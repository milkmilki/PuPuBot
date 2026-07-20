import os
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, call, patch

from tests.helpers import activate_test_instance


TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
TEST_CONTEXT = activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)

from pupu.actor import InstanceActor
from pupu.actor.types import ActorOutboundTarget
from pupu.memory import (
    create_scheduled_task,
    get_due_scheduled_tasks,
    init_db,
    list_scheduled_tasks,
    reset_session,
)
from pupu.scheduler import (
    _is_wait_followup_task,
    _latest_message_is_user,
    _run_due_tasks_with_sender,
)
from pupu.message_sources import SCHEDULED
from pupu.sessions import OWNER_SESSION


async def _no_sleep(_seconds):
    return None


class SchedulerSendTests(unittest.IsolatedAsyncioTestCase):
    def _actor_with_fake_transport(self) -> InstanceActor:
        actor = InstanceActor(TEST_CONTEXT, preflight=False)
        actor.context = replace(actor.context, qq_mode="napcat")
        actor._transport = type(
            "FakeTransport",
            (),
            {
                "send_private_text": AsyncMock(),
                "send_group_text": AsyncMock(),
            },
        )()
        return actor

    async def test_actor_private_send_splits_lines(self):
        actor = self._actor_with_fake_transport()
        with patch("pupu.actor.instance_actor.asyncio.sleep", _no_sleep):
            await actor.send_text(
                ActorOutboundTarget(session_id=OWNER_SESSION, user_id="123"),
                "第一句\n第二句\n\n第三句",
            )

        self.assertEqual(
            actor._transport.send_private_text.await_args_list,
            [call("123", "第一句"), call("123", "第二句"), call("123", "第三句")],
        )

    async def test_actor_group_send_splits_lines_and_prefixes_at_once(self):
        actor = self._actor_with_fake_transport()
        with patch("pupu.actor.instance_actor.asyncio.sleep", _no_sleep):
            await actor.send_text(
                ActorOutboundTarget(
                    session_id="group_456",
                    group_id="456",
                    reply_at_user_id="123",
                ),
                "第一句\n第二句",
            )

        self.assertEqual(
            actor._transport.send_group_text.await_args_list,
            [call("456", "[CQ:at,qq=123] 第一句"), call("456", "第二句")],
        )

    async def test_sender_scheduler_tick_uses_transport_neutral_sender(self):
        task = {
            "id": 99,
            "session_id": OWNER_SESSION,
            "title": "near due",
            "instruction": "say hi",
            "run_at": "2026-05-01T09:00:00",
            "repeat_kind": "once",
            "interval_seconds": None,
        }
        sent = []

        async def sender(session_id, text):
            sent.append((session_id, text))

        with patch("pupu.scheduler.get_due_scheduled_tasks", return_value=[task]):
            with patch("pupu.scheduler.finalize_scheduled_task", return_value=True) as mock_finalize:
                with patch("pupu.agent.chat", return_value="reply") as mock_chat:
                    await _run_due_tasks_with_sender(sender)

        self.assertEqual(sent, [(OWNER_SESSION, "reply")])
        self.assertEqual(mock_chat.call_args.args[5], SCHEDULED)
        self.assertEqual(mock_chat.call_args.kwargs["context_session"], OWNER_SESSION)
        self.assertEqual(mock_chat.call_args.kwargs["identity_session"], OWNER_SESSION)
        mock_finalize.assert_called_once()

    async def test_send_failure_does_not_generate_same_task_again(self):
        task = {
            "id": 100,
            "session_id": OWNER_SESSION,
            "title": "near due",
            "instruction": "say hi",
            "run_at": "2026-05-01T09:00:00",
            "repeat_kind": "once",
            "interval_seconds": None,
        }
        active = True

        def get_due_tasks(_before_iso, _limit):
            return [task] if active else []

        def finalize_task(*_args, **_kwargs):
            nonlocal active
            active = False
            return True

        async def disconnected_sender(_session_id, _text):
            raise ConnectionError("NapCat is not connected")

        with patch("pupu.scheduler.get_due_scheduled_tasks", side_effect=get_due_tasks):
            with patch("pupu.scheduler.finalize_scheduled_task", side_effect=finalize_task):
                with patch("pupu.agent.chat", return_value="reply") as mock_chat:
                    await _run_due_tasks_with_sender(disconnected_sender)
                    await _run_due_tasks_with_sender(disconnected_sender)

        mock_chat.assert_called_once()


class SchedulerGuardTests(unittest.TestCase):
    def test_is_wait_followup_task_detects_prefix(self):
        self.assertTrue(_is_wait_followup_task({"title": "wait_followup:owner"}))
        self.assertTrue(_is_wait_followup_task({"title": "WAIT_FOLLOWUP:any"}))
        self.assertFalse(_is_wait_followup_task({"title": "提醒"}))

    def test_latest_message_is_user(self):
        with patch("pupu.scheduler.get_recent_messages", return_value=[{"role": "user"}]):
            self.assertTrue(_latest_message_is_user(OWNER_SESSION))
        with patch("pupu.scheduler.get_recent_messages", return_value=[{"role": "assistant"}]):
            self.assertFalse(_latest_message_is_user(OWNER_SESSION))
        with patch("pupu.scheduler.get_recent_messages", return_value=[]):
            self.assertFalse(_latest_message_is_user(OWNER_SESSION))


class SchedulerDueWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session_id = f"test_scheduler_due_{self._testMethodName}"
        reset_session(self.session_id)

    def test_due_task_within_one_hour_is_returned(self):
        task_id = create_scheduled_task(
            self.session_id,
            "near due",
            "near due instruction",
            "2026-05-01T09:00:00",
            "once",
            None,
        )

        tasks = get_due_scheduled_tasks("2026-05-01T10:00:00", 20)

        self.assertIn(task_id, [int(task["id"]) for task in tasks])

    def test_due_task_older_than_one_hour_is_skipped_and_removed(self):
        task_id = create_scheduled_task(
            self.session_id,
            "stale due",
            "stale due instruction",
            "2026-05-01T08:59:59",
            "once",
            None,
        )

        tasks = get_due_scheduled_tasks("2026-05-01T10:00:00", 20)

        self.assertNotIn(task_id, [int(task["id"]) for task in tasks])
        self.assertEqual(list_scheduled_tasks(self.session_id), [])

    def test_missed_recurring_task_advances_without_triggering(self):
        task_id = create_scheduled_task(
            self.session_id,
            "daily stale due",
            "daily stale due instruction",
            "2026-05-01T08:00:00",
            "daily",
            None,
        )

        tasks = get_due_scheduled_tasks("2026-05-01T10:00:00", 20)
        remaining = list_scheduled_tasks(self.session_id)

        self.assertNotIn(task_id, [int(task["id"]) for task in tasks])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["run_at"], "2026-05-02T08:00:00")


if __name__ == "__main__":
    unittest.main()
