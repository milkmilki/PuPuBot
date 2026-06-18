import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_storage_split.db"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)

from pupu.memory import (
    _get_conn,
    get_person_fact_map,
    get_recent_messages,
    init_db,
    reset_session,
    save_message,
    upsert_person_facts,
)
from pupu.storage.people import person_from_session


class StorageSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.context_one = f"context_one_{self._testMethodName}"
        self.context_two = f"context_two_{self._testMethodName}"
        self.identity = f"identity_{self._testMethodName}"
        for sid in (self.context_one, self.context_two, self.identity):
            reset_session(sid)

    def test_identity_facts_are_shared_across_contexts(self):
        subject_key = person_from_session(self.identity)
        upsert_person_facts(
            {"喜欢": "草莓"},
            default_subject_person_key=subject_key,
            legacy_session_id=self.identity,
        )

        self.assertEqual(get_person_fact_map(subject_key), {"喜欢": "草莓"})
        self.assertEqual(get_person_fact_map(person_from_session(self.context_one)), {})

    def test_legacy_fact_tables_are_not_created(self):
        conn = _get_conn()
        try:
            table_names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertNotIn("user_facts", table_names)
        self.assertNotIn("self_facts", table_names)

    def test_context_messages_do_not_cross_between_contexts(self):
        save_message("user", "from one", "ignored", context_session=self.context_one)
        save_message("user", "from two", "ignored", context_session=self.context_two)

        self.assertEqual(
            [row["content"] for row in get_recent_messages(10, context_session=self.context_one)],
            ["from one"],
        )
        self.assertEqual(
            [row["content"] for row in get_recent_messages(10, context_session=self.context_two)],
            ["from two"],
        )


if __name__ == "__main__":
    unittest.main()
