import unittest

from pupu.review_parser import (
    _normalize_batch_review_result,
    _parse_batch_review_result,
)


class ReviewParserTests(unittest.TestCase):
    def test_parse_batch_review_result_handles_fences_and_trailing_commas(self):
        raw = """```json
{
  "summary": "talked about movies",
  "familiarity_delta": 2,
  "fact_updates": [{"action": "create", "subject": "小夫", "scope": "person", "key": "favorite_genre", "value": "fantasy",},],
  "event_updates": [],
  "task_updates": []
}
```"""

        parsed = _parse_batch_review_result(raw)

        self.assertEqual(parsed["summary"], "talked about movies")
        self.assertEqual(parsed["familiarity_delta"], 2)
        self.assertEqual(
            parsed["fact_updates"],
            [
                {
                    "action": "create",
                    "subject": "小夫",
                    "object": "",
                    "scope": "person",
                    "key": "favorite_genre",
                    "value": "fantasy",
                    "confidence": 1.0,
                }
            ],
        )

    def test_parse_batch_review_result_repairs_unescaped_quotes_inside_strings(self):
        raw = """```json
{
  "summary": "用户用"永远在一起"表达想做一辈子朋友。",
  "familiarity_delta": 8,
  "fact_updates": [],
  "event_updates": [],
  "task_updates": []
}
```"""

        parsed = _parse_batch_review_result(raw)

        self.assertIn('"永远在一起"', parsed["summary"])
        self.assertEqual(parsed["familiarity_delta"], 8)

    def test_normalize_filters_structured_fact_values(self):
        parsed = _normalize_batch_review_result(
            {
                "summary": "整理事实。",
                "familiarity_delta": 0,
                "fact_updates": [
                    {"action": "create", "subject": "小夫", "scope": "person", "key": "爱好", "value": ["画画"]},
                    {"action": "create", "subject": "小夫", "scope": "person", "key": "昵称", "value": "小夫"},
                    {"action": "create", "subject": "璐璐", "scope": "person", "key": "会做饭", "value": True},
                ],
                "event_updates": [],
                "task_updates": [],
            }
        )

        self.assertEqual(len(parsed["fact_updates"]), 1)
        self.assertEqual(parsed["fact_updates"][0]["action"], "create")
        self.assertEqual(parsed["fact_updates"][0]["key"], "昵称")

    def test_normalize_accepts_update_existing_fact_updates(self):
        parsed = _normalize_batch_review_result(
            {
                "summary": "整理事实。",
                "familiarity_delta": 99,
                "fact_updates": [
                    {"action": "update_existing", "fact_id": 12, "value": "小夫是光头，没有刘海", "confidence": 0.9},
                    {"action": "create", "subject": "小夫", "scope": "person", "key": "近况", "value": "小夫在调整记忆系统"},
                ],
                "event_updates": [],
                "task_updates": [],
            }
        )

        self.assertEqual(parsed["familiarity_delta"], 20)
        self.assertEqual(
            parsed["fact_updates"],
            [
                {
                    "action": "update_existing",
                    "fact_id": 12,
                    "value": "小夫是光头，没有刘海",
                    "confidence": 0.9,
                },
                {
                    "action": "create",
                    "subject": "小夫",
                    "object": "",
                    "scope": "person",
                    "key": "近况",
                    "value": "小夫在调整记忆系统",
                    "confidence": 1.0,
                },
            ],
        )

    def test_normalize_non_dict_returns_empty_shape(self):
        self.assertEqual(
            _normalize_batch_review_result([]),
            {
                "summary": "",
                "familiarity_delta": 0,
                "fact_updates": [],
                "event_updates": [],
                "task_updates": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
