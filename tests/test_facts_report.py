import os
from pathlib import Path
import unittest
from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_pupu.db"
TEST_BACKUP_DIR = Path(__file__).resolve().parent / "_tmp" / "backups"
activate_test_instance(TEST_DB_PATH)
os.environ["PUPU_BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"

from pupu.facts_report import format_facts_report
from pupu.memory import (
    get_person_facts,
    init_db,
    reset_session,
    upsert_person_facts,
)
from pupu.storage.people import INSTANCE_PERSON_KEY, person_from_session


class FactsReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        activate_test_instance(
            TEST_DB_PATH,
            instance_id=f"facts-report-{self._testMethodName}",
            fresh=True,
        )
        init_db()
        os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"
        os.environ.pop("PUPU_SEMANTIC_INDEX_EMBED_API_KEY", None)
        self.session_id = f"test_facts_report_{self._testMethodName}"
        reset_session(self.session_id)

    def test_new_session_report_includes_instance_facts(self):
        report = format_facts_report(self.session_id)

        self.assertIn("Test PuPu facts", report)
        self.assertIn("喜欢的音乐", report)
        self.assertNotIn(f"{self.session_id} facts", report)

    def test_report_includes_owner_and_instance_facts(self):
        subject_key = person_from_session(self.session_id)
        upsert_person_facts(
            {"身份": "读研学生"},
            default_subject_person_key=subject_key,
        )
        upsert_person_facts(
            {"自称": "姐姐"},
            default_subject_person_key=INSTANCE_PERSON_KEY,
        )

        report = format_facts_report(self.session_id)

        self.assertIn(f"{self.session_id} facts 1 条", report)
        self.assertIn("1. 身份: 读研学生", report)
        self.assertIn("Test PuPu facts", report)
        self.assertIn("自称: 姐姐", report)

    def test_person_facts_can_store_relationship_facts(self):
        other_session = self.session_id + "_other"
        upsert_person_facts(
            [
                {
                    "subject": "owner",
                    "object": "instance",
                    "scope": "relationship",
                    "key": "称呼",
                    "value": "用户会叫实例姐姐",
                },
                {
                    "subject": other_session,
                    "object": "instance",
                    "scope": "relationship",
                    "key": "称呼",
                    "value": "另一个人会叫实例老师",
                }
            ],
        )

        facts = get_person_facts(
            subject_person_keys=["owner", "instance"],
            include_relationships=True,
        )

        self.assertIn(
            ("owner", "instance", "relationship", "称呼", "用户会叫实例姐姐"),
            {
                (
                    row["subject_person_key"],
                    row["object_person_key"],
                    row["scope"],
                    row["fact_key"],
                    row["fact_value"],
                )
                for row in facts
            },
        )
        self.assertIn(
            (other_session, "instance", "relationship", "称呼", "另一个人会叫实例老师"),
            {
                (
                    row["subject_person_key"],
                    row["object_person_key"],
                    row["scope"],
                    row["fact_key"],
                    row["fact_value"],
                )
                for row in facts
            },
        )

    def test_unknown_fact_scope_falls_back_to_supported_scopes(self):
        upsert_person_facts(
            [
                {
                    "subject": "owner",
                    "scope": "group",
                    "key": "称呼",
                    "value": "用户在群里自称小夫",
                },
                {
                    "subject": "owner",
                    "object": "instance",
                    "scope": "group",
                    "key": "互动习惯",
                    "value": "用户会在群里和实例开玩笑",
                },
            ],
        )

        facts = get_person_facts(
            subject_person_keys=["owner", "instance"],
            include_relationships=True,
        )
        scopes = {
            (row["subject_person_key"], row["object_person_key"], row["fact_key"]): row["scope"]
            for row in facts
        }

        self.assertEqual(scopes[("owner", "", "称呼")], "person")
        self.assertEqual(scopes[("owner", "instance", "互动习惯")], "relationship")

    def test_facts_search_finds_related_person_fact(self):
        subject_key = person_from_session(self.session_id)
        upsert_person_facts(
            {"外貌": "小夫是光头，没有刘海"},
            default_subject_person_key=subject_key,
        )

        report = format_facts_report(self.session_id, query="search 刘海")

        self.assertIn("相关 facts", report)
        self.assertIn("外貌: 小夫是光头，没有刘海", report)

    def test_facts_search_debug_includes_score_details(self):
        subject_key = person_from_session(self.session_id)
        upsert_person_facts(
            {"外貌": "小夫是光头，没有刘海"},
            default_subject_person_key=subject_key,
        )

        report = format_facts_report(self.session_id, query="search --debug 刘海")

        self.assertIn("debug:", report)
        self.assertIn("used_semantic_index=", report)


if __name__ == "__main__":
    unittest.main()
