import os
import unittest
from unittest.mock import Mock, patch

from pupu.tooling.servers import web


class WebSearchProviderChainTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "PUPU_WEB_SEARCH_PROVIDER",
            "PUPU_WEB_SEARCH_FALLBACKS",
            "PUPU_TAVILY_API_KEY",
            "TAVILY_API_KEY",
            "PUPU_TAVILY_SEARCH_DEPTH",
            "PUPU_TAVILY_TOPIC",
            "PUPU_TAVILY_DAYS",
            "PUPU_TAVILY_TIME_RANGE",
        ):
            os.environ.pop(name, None)

    def test_provider_chain_uses_fallbacks_when_primary_empty(self):
        with patch.dict(os.environ, {"PUPU_WEB_SEARCH_FALLBACKS": "tavily,ddg_html"}):
            self.assertEqual(web._search_provider_chain(), ["tavily", "ddg_html"])

    def test_provider_chain_defaults_to_tavily_then_ddg_html(self):
        with patch.dict(
            os.environ,
            {
                "PUPU_WEB_SEARCH_PROVIDER": "",
                "PUPU_WEB_SEARCH_FALLBACKS": "",
            },
        ):
            self.assertEqual(web._search_provider_chain(), ["tavily", "ddg_html"])

    def test_provider_chain_dedupes_and_normalizes_aliases(self):
        with patch.dict(
            os.environ,
            {
                "PUPU_WEB_SEARCH_PROVIDER": "duckduckgo",
                "PUPU_WEB_SEARCH_FALLBACKS": "ddg_html,legacy",
            },
        ):
            self.assertEqual(web._search_provider_chain(), ["ddg_html", "legacy_ddgs"])

    def test_tavily_search_maps_results(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com",
                    "content": "Snippet",
                    "published_date": "2026-05-24",
                }
            ]
        }

        with patch.dict(os.environ, {"PUPU_TAVILY_API_KEY": "test-key"}):
            with patch("pupu.tooling.servers.web.httpx.post", return_value=response) as mock_post:
                results, backend = web._search_with_tavily("hello", 3)

        self.assertEqual(backend, "tavily")
        self.assertEqual(results[0]["href"], "https://example.com")
        self.assertIn("Snippet", results[0]["body"])
        self.assertIn("2026-05-24", results[0]["body"])
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-key")

    def test_web_search_falls_back_after_tavily_failure(self):
        with patch.dict(os.environ, {"PUPU_WEB_SEARCH_FALLBACKS": "tavily,ddg_html"}):
            with patch(
                "pupu.tooling.servers.web._search_with_tavily",
                side_effect=RuntimeError("no key"),
            ):
                with patch(
                    "pupu.tooling.servers.web._search_with_duckduckgo_html",
                    return_value=(
                        [
                            {
                                "title": "Fallback",
                                "href": "https://fallback.test",
                                "body": "ok",
                            }
                        ],
                        "duckduckgo_html_page",
                    ),
                ):
                    text = web.web_search("hello")

        self.assertIn("Fallback", text)
        self.assertIn("https://fallback.test", text)


if __name__ == "__main__":
    unittest.main()
