import os
from pathlib import Path
import unittest

from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"

import pupu.agent as agent
import pupu.prompt_format as prompt_format
from pupu.prompt_format import (
    _known_review_people_map,
    _review_speaker_name_for_message,
    _speaker_payload_from_message,
)

_OWNER_PAYLOAD = (
    '[{"person_key":"owner","display_name":"小夫","qq_id":"424225912","kind":"owner"},'
    '{"person_key":"qq:3853876778","display_name":"仆仆","qq_id":"3853876778","kind":"qq"}]'
)


class SpeakerPayloadParsingTests(unittest.TestCase):
    """Locks the JSON branch that was silently dead before the refactor.

    Old agent.py used ``json.loads`` without importing ``json``: every
    ``[``-prefixed ``speaker_key`` raised ``NameError`` and was swallowed,
    so this parser always returned ``[]``. After extraction to
    ``prompt_format`` (which imports ``json``) the branch runs for real.
    """

    def test_valid_json_array_parses_to_dicts(self):
        payload = _speaker_payload_from_message({"speaker_key": _OWNER_PAYLOAD})
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["person_key"], "owner")
        self.assertEqual(payload[1]["person_key"], "qq:3853876778")

    def test_non_dict_entries_are_filtered_out(self):
        payload = _speaker_payload_from_message(
            {"speaker_key": '[{"person_key":"owner"}, "noise", 42]'}
        )
        self.assertEqual(payload, [{"person_key": "owner"}])

    def test_non_bracket_prefix_returns_empty(self):
        self.assertEqual(_speaker_payload_from_message({"speaker_key": "owner"}), [])

    def test_malformed_json_returns_empty(self):
        self.assertEqual(_speaker_payload_from_message({"speaker_key": "[not json"}), [])

    def test_missing_speaker_key_returns_empty(self):
        self.assertEqual(_speaker_payload_from_message({}), [])


class ReviewSpeakerNameFromPayloadTests(unittest.TestCase):
    """The observable effect of the reactivated payload parser.

    ``known_names`` here maps only the payload's ``person_key`` values, not
    the raw ``speaker_key`` or ``speaker_qq`` on the item. So a correct
    multi-speaker label can ONLY be produced by parsing the payload. Under
    the old (dead) parser the function fell through to ``speaker_name``.
    """

    def test_multi_person_label_is_derived_from_payload(self):
        item = {
            "role": "user",
            "content": "群里聊天",
            "speaker_key": _OWNER_PAYLOAD,
            "speaker_qq": "",
            "speaker_name": "路人甲",  # old fallback; must NOT appear
        }
        known = {"owner": "小夫", "qq:3853876778": "仆仆"}
        label = _review_speaker_name_for_message(item, known, "璐璐")
        self.assertEqual(label, "小夫 / 仆仆")
        self.assertNotEqual(label, "路人甲")

    def test_more_than_three_speakers_are_truncated(self):
        payload = (
            '[{"person_key":"a"},{"person_key":"b"},'
            '{"person_key":"c"},{"person_key":"d"}]'
        )
        item = {"role": "user", "content": "x", "speaker_key": payload}
        known = {"a": "甲", "b": "乙", "c": "丙", "d": "丁"}
        label = _review_speaker_name_for_message(item, known, "璐璐")
        self.assertEqual(label, "甲 / 乙 / 丙 等")

    def test_known_names_map_indexes_person_key_and_qq(self):
        people = [
            {"person_key": "owner", "display_name": "小夫", "qq_id": "424225912"},
            {"person_key": "qq:3853876778", "display_name": "仆仆", "qq_id": "3853876778"},
        ]
        known = _known_review_people_map(people)
        self.assertEqual(known["owner"], "小夫")
        self.assertEqual(known["qq:424225912"], "小夫")
        self.assertEqual(known["qq:3853876778"], "仆仆")


class ReExportContractTests(unittest.TestCase):
    """Guards the import contracts the refactor relies on.

    Constraint B: tests and production code do ``from pupu.agent import X``
    for names physically living in ``prompt_format``. agent.py must
    re-export them as the *same object*.
    Constraint A: ``_format_turn_timestamp`` uses ``datetime`` (monkeypatched
    via ``pupu.agent.datetime``) and MUST stay defined in agent.py, not be
    imported from prompt_format.
    """

    RE_EXPORTED = (
        "_format_chat_history_for_prompt",
        "_format_event_thread_candidates_for_review",
        "_format_message_content_for_prompt",
        "_speaker_payload_from_message",
        "_review_speaker_name_for_message",
        "_merge_people_for_prompt",
    )

    def test_agent_re_exports_are_same_object(self):
        for name in self.RE_EXPORTED:
            with self.subTest(name=name):
                self.assertIs(getattr(agent, name), getattr(prompt_format, name))

    def test_turn_timestamp_stays_in_agent(self):
        self.assertTrue(hasattr(agent, "_format_turn_timestamp"))
        self.assertFalse(hasattr(prompt_format, "_format_turn_timestamp"))

    def test_prompt_format_does_not_import_agent(self):
        self.assertFalse(hasattr(prompt_format, "chat"))
        self.assertFalse(hasattr(prompt_format, "_chat_impl"))


if __name__ == "__main__":
    unittest.main()
