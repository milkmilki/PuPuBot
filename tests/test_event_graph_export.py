import unittest

from pupu.event_graph_export import (
    _build_event_graph_html,
    _load_event_graph_template,
)


class EventGraphExportTests(unittest.TestCase):
    def test_template_exposes_only_render_contract_slots(self):
        template = _load_event_graph_template()

        self.assertEqual(1, template.count("__TITLE__"))
        self.assertEqual(1, template.count("__DATA_JSON__"))

    def test_render_escapes_title_and_script_closing_sequence(self):
        rendered = _build_event_graph_html(
            {
                "session_id": "<owner>",
                "details": "</script><script>alert('unsafe')</script>",
            }
        )

        self.assertIn("<title>PuPu 事件图谱 - &lt;owner&gt;</title>", rendered)
        self.assertNotIn("__TITLE__", rendered)
        self.assertNotIn("__DATA_JSON__", rendered)
        self.assertNotIn("</script><script>alert", rendered)
        self.assertIn("<\\/script><script>alert", rendered)


if __name__ == "__main__":
    unittest.main()
