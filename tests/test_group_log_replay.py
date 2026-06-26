import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_group_log_replay.db"
activate_test_instance(TEST_DB_PATH, display_name="璐璐", instance_id="group-log-replay")
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"

from pupu.agent import REVIEW_INTERVAL, _maybe_batch_review, chat
from pupu.memory import (
    get_event_thread_steps,
    get_event_threads,
    get_person_facts,
    init_db,
    reset_session,
    save_message_with_speaker,
    set_familiarity,
)
from pupu.message_sources import CHAT
from pupu.sessions import OWNER_SESSION
from pupu.storage import get_conn, upsert_person
from pupu_console import arbitrator


GROUP_SESSION = "group_1103489921"
OWNER_QQ = "424225912"
PUPU_QQ = "3853876778"
OWNER_RAW_NICK = "钮钴禄·大家大宁"


def _speaker_payload(*speakers: dict[str, str]) -> str:
    return json.dumps(list(speakers), ensure_ascii=False, separators=(",", ":"))


OWNER_PERSON = {
    "person_key": "owner",
    "display_name": OWNER_RAW_NICK,
    "qq_id": OWNER_QQ,
    "kind": "owner",
}
PUPU_PERSON = {
    "person_key": f"qq:{PUPU_QQ}",
    "display_name": "仆仆",
    "qq_id": PUPU_QQ,
    "kind": "qq",
}
LULU_PERSON = {
    "person_key": "instance",
    "display_name": "璐璐",
    "qq_id": "",
    "kind": "instance",
}


class GroupLogReplayTests(unittest.TestCase):
    def setUp(self):
        activate_test_instance(
            TEST_DB_PATH,
            display_name="璐璐",
            instance_id=f"group-log-replay-{self._testMethodName}",
            fresh=True,
        )
        os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"
        init_db()
        reset_session(GROUP_SESSION)
        reset_session(OWNER_SESSION)
        reset_session(f"private_{PUPU_QQ}")
        set_familiarity(100, session_id=OWNER_SESSION)
        set_familiarity(50, session_id=f"private_{PUPU_QQ}")
        self._setup_people()

    def _setup_people(self) -> None:
        conn = get_conn()
        try:
            upsert_person(
                conn,
                "owner",
                kind="owner",
                display_name="小夫",
                qq_id=OWNER_QQ,
                aliases=[OWNER_RAW_NICK],
            )
            upsert_person(
                conn,
                f"qq:{PUPU_QQ}",
                kind="qq",
                display_name="仆仆",
                qq_id=PUPU_QQ,
            )
            upsert_person(
                conn,
                "instance",
                kind="instance",
                display_name="璐璐",
            )
            conn.commit()
        finally:
            conn.close()

    def _save_replay_rows(self, *, fill_to_review_interval: bool = False) -> None:
        owner_pupu_payload = _speaker_payload(OWNER_PERSON, PUPU_PERSON)
        save_message_with_speaker(
            "user",
            "[时间: 2026-06-18 周四 18:38] "
            f"[{OWNER_RAW_NICK}(QQ:{OWNER_QQ})] 不是睡觉\n"
            f"[仆仆(QQ:{PUPU_QQ})] 仆仆：先把今天的活干完再说晚上",
            GROUP_SESSION,
            source=CHAT,
            speaker_key=owner_pupu_payload,
            speaker_name=OWNER_RAW_NICK,
            speaker_qq=OWNER_QQ,
        )
        save_message_with_speaker(
            "assistant",
            "璐璐：好了好了",
            GROUP_SESSION,
            source=CHAT,
            speaker_key="instance",
            speaker_name="璐璐",
        )
        replay_rows = [
            ("owner", OWNER_RAW_NICK, OWNER_QQ, "我代码写完了呀"),
            ("qq:" + PUPU_QQ, "仆仆", PUPU_QQ, "大宁你先想想自己的代码写完没"),
            ("qq:" + PUPU_QQ, "仆仆", PUPU_QQ, "跟璐璐一样 今晚看你表现再说"),
            ("owner", OWNER_RAW_NICK, OWNER_QQ, "好好好我晚上表现"),
            ("instance", "璐璐", "", "行，晚上再说"),
        ]
        for speaker_key, speaker_name, speaker_qq, content in replay_rows:
            save_message_with_speaker(
                "assistant" if speaker_key == "instance" else "user",
                content,
                GROUP_SESSION,
                source=CHAT,
                speaker_key=speaker_key,
                speaker_name=speaker_name,
                speaker_qq=speaker_qq,
            )
        if not fill_to_review_interval:
            return
        saved_count = 2 + len(replay_rows)
        for index in range(saved_count, REVIEW_INTERVAL):
            save_message_with_speaker(
                "user",
                f"填充消息 {index}",
                GROUP_SESSION,
                source=CHAT,
                speaker_key="owner",
                speaker_name=OWNER_RAW_NICK,
                speaker_qq=OWNER_QQ,
            )

    def test_chat_prompt_replays_real_group_log_as_natural_qq_records(self):
        self._save_replay_rows()
        with (
            patch("pupu.agent.get_pupu_name", return_value="璐璐"),
            patch("pupu.persona.builder.get_pupu_name", return_value="璐璐"),
            patch("pupu.agent.chat_complete", return_value='{"content":"别在群里嚎","should_wait":false}') as mock_chat,
            patch("pupu.agent._maybe_batch_review", return_value=None),
        ):
            reply = chat(
                "继续说",
                session_id=GROUP_SESSION,
                identity_session=OWNER_SESSION,
                is_admin=False,
                persist_user=False,
                speaker_key=_speaker_payload(OWNER_PERSON),
                speaker_name=OWNER_RAW_NICK,
                speaker_qq=OWNER_QQ,
            )

        self.assertEqual(reply, "别在群里嚎")
        kwargs = mock_chat.call_args.kwargs
        self.assertIn("## 当前群聊人物", kwargs["system"])
        self.assertIn("你是璐璐。", kwargs["system"])
        self.assertIn("小夫：与你的关系是恋人。", kwargs["system"])
        self.assertIn("仆仆：与你的关系是朋友。", kwargs["system"])

        joined = "\n".join(item["content"] for item in kwargs["messages"])
        self.assertIn("[时间: 2026-06-18 周四 18:38] 小夫：不是睡觉", joined)
        self.assertIn("[时间: 2026-06-18 周四 18:38] 仆仆：先把今天的活干完再说晚上", joined)
        self.assertIn("仆仆：大宁你先想想自己的代码写完没", joined)
        self.assertIn("好了好了", joined)
        assistant_messages = [item["content"] for item in kwargs["messages"] if item["role"] == "assistant"]
        self.assertIn("好了好了", assistant_messages)
        self.assertNotIn("璐璐：好了好了", assistant_messages)
        self.assertNotIn(OWNER_RAW_NICK, joined)
        self.assertNotIn(f"QQ:{OWNER_QQ}", joined)
        self.assertNotIn("“恋人”", joined)
        self.assertNotIn("“朋友”", joined)
        self.assertNotIn("“自己”", joined)
        self.assertNotIn("仆仆：仆仆：", joined)

    def test_batch_review_replays_real_group_log_with_distinct_people(self):
        self._save_replay_rows(fill_to_review_interval=True)
        raw = json.dumps(
            {
                "summary": "2026年6月18日傍晚，小夫在群聊中和仆仆、璐璐约定晚上看表现。",
                "fact_updates": [
                    {
                        "action": "create",
                        "subject": "小夫",
                        "object": "仆仆",
                        "scope": "relationship",
                        "key": "日常称呼",
                        "value": "小夫有时会直接在群里叫仆仆姐姐。",
                        "confidence": 0.8,
                    }
                ],
                "event_updates": [
                    {
                        "action": "create_thread",
                        "thread_key": "real-group-20260618-evening-plan",
                        "title": "2026年6月18日晚上小夫与仆仆、璐璐的约定",
                        "summary": "小夫在群聊中说晚上会好好表现，仆仆和璐璐要求他先干完活。",
                        "people": ["仆仆", "小夫", "璐璐"],
                        "event_time": "2026-06-18T18:38:00",
                        "followup_hint": "晚上后可询问小夫表现如何。",
                        "confidence": 0.9,
                    }
                ],
                "task_updates": [],
            },
            ensure_ascii=False,
        )

        with (
            patch("pupu.agent.get_pupu_name", return_value="璐璐"),
            patch("pupu.agent.json_task", return_value=raw) as mock_json_task,
        ):
            _maybe_batch_review(GROUP_SESSION, identity_session=OWNER_SESSION)

        review_input = mock_json_task.call_args.kwargs["user_content"]
        self.assertIn("[时间: 2026-06-18 周四 18:38] 小夫：不是睡觉 <end>", review_input)
        self.assertIn("[时间: 2026-06-18 周四 18:38] 仆仆：先把今天的活干完再说晚上 <end>", review_input)
        self.assertIn("璐璐：好了好了 <end>", review_input)
        self.assertIn("仆仆：大宁你先想想自己的代码写完没 <end>", review_input)
        self.assertNotIn(OWNER_RAW_NICK, review_input)
        self.assertNotIn(f"QQ:{OWNER_QQ}", review_input)
        self.assertNotIn("qq:424225912", review_input)
        self.assertNotIn("“恋人”", review_input)
        self.assertNotIn("“朋友”", review_input)
        self.assertNotIn("“自己”", review_input)
        self.assertNotIn("仆仆：仆仆：", review_input)
        self.assertNotIn("璐璐、璐璐", review_input)

        events = get_event_threads(OWNER_SESSION, limit=5)
        event = next(item for item in events if item["thread_key"] == "real-group-20260618-evening-plan")
        event_people = [name.strip() for name in event["people_label"].split("/") if name.strip()]
        self.assertCountEqual(event_people, ["仆仆", "小夫", "璐璐"])
        self.assertEqual(len(event_people), len(set(event_people)))

        _thread, steps = get_event_thread_steps(OWNER_SESSION, "real-group-20260618-evening-plan")
        step_people = [name.strip() for name in steps[-1]["people_label"].split("/") if name.strip()]
        self.assertCountEqual(step_people, ["仆仆", "小夫", "璐璐"])
        self.assertEqual(len(step_people), len(set(step_people)))

        facts = get_person_facts(subject_person_keys=["owner"], include_relationships=True)
        relationship = next(item for item in facts if item["fact_key"] == "日常称呼")
        self.assertEqual(relationship["subject_display_name"], "小夫")
        self.assertEqual(relationship["object_display_name"], "仆仆")

    def test_arbiter_context_replays_canonical_names_without_qq_or_raw_nick(self):
        messages = [
            {
                "message_id": "1",
                "speaker_qq": OWNER_QQ,
                "speaker_person_key": "owner",
                "speaker_name": "小夫",
                "speaker_is_bot": False,
                "text": "大家晚上看我表现",
            },
            {
                "message_id": "2",
                "speaker_qq": PUPU_QQ,
                "speaker_person_key": f"qq:{PUPU_QQ}",
                "speaker_name": "仆仆",
                "speaker_is_bot": True,
                "text": "你先把今天的活干完",
            },
            {
                "message_id": "3",
                "speaker_qq": "",
                "speaker_person_key": "instance",
                "speaker_name": "璐璐",
                "speaker_is_bot": True,
                "text": "好了好了",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PUPU_REPO_ROOT"] = tmp
            try:
                context, _targets, since = arbitrator._build_recent_context(messages)
            finally:
                os.environ.pop("PUPU_REPO_ROOT", None)

        self.assertEqual(since, "3")
        self.assertIn("[小夫] 大家晚上看我表现", context)
        self.assertIn("[bot 仆仆] 你先把今天的活干完", context)
        self.assertIn("[bot 璐璐] 好了好了", context)
        self.assertNotIn(OWNER_RAW_NICK, context)
        self.assertNotIn(OWNER_QQ, context)
        self.assertNotIn("qq:", context)


if __name__ == "__main__":
    unittest.main()
