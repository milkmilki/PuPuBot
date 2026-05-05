import os
from pathlib import Path
import unittest

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_storage_split.db"
os.environ["PUPU_DB_PATH"] = str(TEST_DB_PATH)

from pupu.memory import (
    get_recent_messages,
    get_user_facts,
    init_db,
    reset_session,
    save_message,
    upsert_user_facts,
)


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
        upsert_user_facts({"喜欢": "草莓"}, "ignored", identity_session=self.identity)

        self.assertEqual(
            get_user_facts("ignored", identity_session=self.identity),
            {"喜欢": "草莓"},
        )
        self.assertEqual(get_user_facts(self.context_one), {})

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
