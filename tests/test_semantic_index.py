import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.helpers import activate_test_instance

TEST_DB_PATH = Path(__file__).resolve().parent / "_tmp" / "test_semantic_index.db"
activate_test_instance(TEST_DB_PATH, display_name="Lulu", instance_id="semantic-index")
os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "true"
os.environ["PUPU_SEMANTIC_INDEX_EMBED_API_KEY"] = "test-key"

from pupu.memory import (
    _get_conn,
    init_db,
    reset_session,
    save_summary,
    upsert_person_facts,
)
from pupu.semantic_index import (
    clear_semantic_index,
    clear_semantic_session,
    recall_memories,
    rebuild_source_cache,
    run_semantic_tidy,
    sync_review_memory,
)
from pupu.semantic_index.projection import build_review_entries, expected_source_entries
from pupu.semantic_index.store import list_cards
from pupu.semantic_index.vector import cosine_similarity, pack_vector, unpack_vector
from pupu.storage.people import person_from_session


def _embedding_for(text: str) -> tuple[list[float], str]:
    text = str(text or "")
    return [
        1.0 if "光头" in text or "刘海" in text else 0.0,
        1.0 if "昵称" in text or "小夫" in text else 0.0,
        1.0,
    ], "test-embed"


class SemanticIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        activate_test_instance(
            TEST_DB_PATH,
            display_name="Lulu",
            instance_id=f"semantic-index-{self._testMethodName}",
            fresh=True,
        )
        init_db()
        os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "true"
        os.environ["PUPU_SEMANTIC_INDEX_EMBED_API_KEY"] = "test-key"
        self.session_id = f"semantic_{self._testMethodName}"
        reset_session(self.session_id)
        clear_semantic_index()

    def tearDown(self) -> None:
        os.environ["PUPU_SEMANTIC_INDEX_ENABLED"] = "false"
        os.environ.pop("PUPU_SEMANTIC_INDEX_EMBED_API_KEY", None)

    def test_vector_blob_roundtrip_and_cosine(self) -> None:
        packed = pack_vector([1.0, 2.0, 3.5])

        unpacked = unpack_vector(packed)
        self.assertEqual(len(unpacked), 3)
        self.assertAlmostEqual(sum(value * value for value in unpacked), 1.0, places=6)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_schema_creates_semantic_tables(self) -> None:
        conn = _get_conn()
        try:
            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
        finally:
            conn.close()

        self.assertIn("semantic_cards", tables)
        self.assertIn("semantic_sync_log", tables)
        self.assertNotIn("memu_sync_log", tables)

    def test_projection_builds_readable_cards(self) -> None:
        with patch("pupu.semantic_index.projection.get_pupu_name", return_value="璐璐"):
            entries = build_review_entries(
                summary="小夫说自己醒了。",
                person_facts=[
                    {
                        "subject_display_name": "小夫",
                        "scope": "person",
                        "fact_key": "外貌",
                        "fact_value": "小夫是光头",
                    }
                ],
                event_threads=[
                    {
                        "thread_key": "morning-check",
                        "title": "确认起床",
                        "event_time": "2026-06-26",
                        "details": "小夫醒来后告诉璐璐",
                        "people_label": "小夫 / 璐璐",
                    }
                ],
            )

        texts = [text for _kind, text, _extra in entries]
        self.assertIn("对话摘要（用户 / 璐璐）: 小夫说自己醒了。", texts)
        self.assertIn("小夫 | 外貌: 小夫是光头", texts)
        self.assertTrue(any("相关人物: 小夫 / 璐璐; 2026年6月26日" in text for text in texts))

    def test_sync_review_writes_source_backed_cards(self) -> None:
        save_summary("小夫说自己是光头。", 1, 2, self.session_id)
        person_key = person_from_session(self.session_id)
        facts = upsert_person_facts(
            {"外貌": "小夫是光头，没有刘海"},
            default_subject_person_key=person_key,
            context_session=self.session_id,
            source_msg_start_id=1,
            source_msg_end_id=2,
        )

        with patch("pupu.semantic_index.core.embed_text", side_effect=_embedding_for):
            result = sync_review_memory(
                context_session=self.session_id,
                identity_session=self.session_id,
                start_msg_id=1,
                end_msg_id=2,
                summary="小夫说自己是光头。",
                person_facts=facts,
                event_threads=[],
            )

        self.assertEqual(result.status, "success")
        cards = list_cards()
        self.assertEqual({card.source_type for card in cards}, {"summary", "person_fact"})
        self.assertTrue(all(card.source_key for card in cards))
        self.assertTrue(all(card.embedding_model == "test-embed" for card in cards))

    def test_recall_uses_semantic_card_but_returns_latest_sqlite_truth(self) -> None:
        person_key = person_from_session(self.session_id)
        facts = upsert_person_facts(
            {"外貌": "小夫是光头"},
            default_subject_person_key=person_key,
        )
        with patch("pupu.semantic_index.core.embed_text", side_effect=_embedding_for):
            sync_review_memory(
                context_session=self.session_id,
                identity_session=self.session_id,
                start_msg_id=1,
                end_msg_id=2,
                summary="",
                person_facts=facts,
                event_threads=[],
            )
        upsert_person_facts(
            {"外貌": "小夫是光头，没有刘海"},
            default_subject_person_key=person_key,
        )

        with patch("pupu.semantic_index.core.embed_text", side_effect=_embedding_for):
            recalled = recall_memories(
                query="刘海",
                context_session=self.session_id,
                identity_session=self.session_id,
                history=[],
                limit=1,
            )

        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["source"], "semantic_index")
        self.assertIn("没有刘海", recalled[0]["text"])

    def test_tidy_rebuild_creates_missing_cards(self) -> None:
        person_key = person_from_session(self.session_id)
        upsert_person_facts(
            {"昵称": "小夫希望别人叫自己小夫"},
            default_subject_person_key=person_key,
        )

        with patch("pupu.semantic_index.core.embed_text", side_effect=_embedding_for):
            result = run_semantic_tidy(self.session_id, mode="rebuild")

        self.assertEqual(result["status"], "synced")
        self.assertGreaterEqual(result["created"], 1)
        self.assertTrue(any(card.source_type == "person_fact" for card in list_cards()))

    def test_rebuild_candidates_include_all_summaries_by_default(self) -> None:
        for index in range(85):
            save_summary(
                f"第 {index} 条摘要",
                index * 2 + 1,
                index * 2 + 2,
                self.session_id,
            )

        entries = expected_source_entries(self.session_id)
        summary_entries = [entry for entry in entries if entry[0] == "summary"]

        self.assertEqual(len(summary_entries), 85)
        self.assertTrue(any("第 0 条摘要" in entry[1] for entry in summary_entries))

    def test_clear_semantic_session_only_removes_matching_session_cards(self) -> None:
        other_session = self.session_id + "_other"
        reset_session(other_session)
        save_summary("小夫说自己醒了。", 1, 2, self.session_id)
        save_summary("另一个会话说要出门。", 1, 2, other_session)

        with patch("pupu.semantic_index.core.embed_text", side_effect=_embedding_for):
            sync_review_memory(
                context_session=self.session_id,
                identity_session=self.session_id,
                start_msg_id=1,
                end_msg_id=2,
                summary="小夫说自己醒了。",
                person_facts=[],
                event_threads=[],
            )
            sync_review_memory(
                context_session=other_session,
                identity_session=other_session,
                start_msg_id=1,
                end_msg_id=2,
                summary="另一个会话说要出门。",
                person_facts=[],
                event_threads=[],
            )

        removed = clear_semantic_session(self.session_id)

        self.assertEqual(removed, 1)
        remaining_cards = list_cards()
        self.assertFalse(
            any(card.source_key.startswith(f"summary:{self.session_id}:") for card in remaining_cards)
        )
        self.assertTrue(
            any(card.source_key.startswith(f"summary:{other_session}:") for card in remaining_cards)
        )
        self.assertEqual(len(remaining_cards), 1)

    def test_clear_semantic_index_does_not_require_embedding_key(self) -> None:
        save_summary("小夫说自己是光头。", 1, 2, self.session_id)
        with patch("pupu.semantic_index.core.embed_text", side_effect=_embedding_for):
            sync_review_memory(
                context_session=self.session_id,
                identity_session=self.session_id,
                start_msg_id=1,
                end_msg_id=2,
                summary="小夫说自己是光头。",
                person_facts=[],
                event_threads=[],
            )

        with patch.dict(
            os.environ,
            {
                "PUPU_SEMANTIC_INDEX_ENABLED": "false",
                "PUPU_SEMANTIC_INDEX_EMBED_API_KEY": "",
            },
        ):
            removed = clear_semantic_index()

        self.assertEqual(removed, 1)
        self.assertEqual(list_cards(), [])

    def test_rebuild_reports_disabled_without_api_key(self) -> None:
        with patch.dict(os.environ, {"PUPU_SEMANTIC_INDEX_EMBED_API_KEY": ""}):
            result = rebuild_source_cache(self.session_id)

        self.assertEqual(result["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
