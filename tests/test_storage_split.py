from pathlib import Path
import unittest
from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_storage_split.db"
activate_test_instance(TEST_DB_PATH)

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

    def test_person_facts_legacy_session_column_is_migrated_away(self):
        conn = _get_conn()
        try:
            conn.execute("DROP TABLE IF EXISTS person_facts")
            conn.execute(
                """
                CREATE TABLE person_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_person_key TEXT NOT NULL,
                    object_person_key TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'person',
                    legacy_session_id TEXT NOT NULL DEFAULT '',
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_context_session TEXT NOT NULL DEFAULT '',
                    source_msg_start_id INTEGER,
                    source_msg_end_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """INSERT INTO person_facts (
                       subject_person_key, object_person_key, scope, legacy_session_id,
                       fact_key, fact_value, confidence, source_context_session,
                       created_at, updated_at
                   ) VALUES ('owner', '', 'person', 'owner', '昵称', '小夫', 1.0, '', 't', 't')"""
            )
            conn.commit()
        finally:
            conn.close()

        init_db()

        conn = _get_conn()
        try:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(person_facts)").fetchall()
            }
            row = conn.execute(
                "SELECT subject_person_key, fact_key, fact_value FROM person_facts WHERE fact_key = '昵称'"
            ).fetchone()
        finally:
            conn.close()

        self.assertNotIn("legacy_session_id", columns)
        self.assertIsNotNone(row)
        self.assertEqual(dict(row), {"subject_person_key": "owner", "fact_key": "昵称", "fact_value": "小夫"})

    def test_person_facts_duplicate_rows_are_deduped_before_unique_index(self):
        conn = _get_conn()
        try:
            conn.execute("DROP INDEX IF EXISTS idx_person_facts_key")
            conn.execute("DROP TABLE IF EXISTS person_facts")
            conn.execute(
                """
                CREATE TABLE person_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_person_key TEXT NOT NULL,
                    object_person_key TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'person',
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_context_session TEXT NOT NULL DEFAULT '',
                    source_msg_start_id INTEGER,
                    source_msg_end_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """INSERT INTO person_facts (
                       subject_person_key, object_person_key, scope, fact_key,
                       fact_value, confidence, created_at, updated_at
                   ) VALUES ('instance', '', 'person', '爱好', '旧值', 1.0, 't1', 't1')"""
            )
            conn.execute(
                """INSERT INTO person_facts (
                       subject_person_key, object_person_key, scope, fact_key,
                       fact_value, confidence, created_at, updated_at
                   ) VALUES ('instance', '', 'person', '爱好', '新值', 1.0, 't2', 't2')"""
            )
            conn.commit()
        finally:
            conn.close()

        init_db()

        conn = _get_conn()
        try:
            rows = conn.execute(
                """SELECT fact_value
                   FROM person_facts
                   WHERE subject_person_key = 'instance'
                     AND object_person_key = ''
                     AND scope = 'person'
                     AND fact_key = '爱好'"""
            ).fetchall()
            index_row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_person_facts_key'"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual([row["fact_value"] for row in rows], ["新值"])
        self.assertIsNotNone(index_row)

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
