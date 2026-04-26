"""Web/search tool server."""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from ..base import BuiltinToolServer, ToolContext, ToolSpec


def web_search(query: str, max_results: int = 5) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "没搜到相关结果"
        output = []
        for result in results:
            output.append(
                f"**{result['title']}**\n{result['href']}\n{result['body']}\n"
            )
        return "\n".join(output)
    except Exception as exc:
        return f"搜索出错了：{exc}"


def fetch_url(url: str) -> str:
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if len(text) > 8000:
            text = text[:8000] + "\n\n...(内容太长，已截断)"
        return text
    except Exception as exc:
        return f"抓取网页出错了：{exc}"


def _handle_web_search(tool_input: dict, _context: ToolContext) -> str:
    return web_search(tool_input["query"])


def _handle_fetch_url(tool_input: dict, _context: ToolContext) -> str:
    return fetch_url(tool_input["url"])


WEB_SERVER = BuiltinToolServer(
    name="web",
    description="Web search and page fetch tools.",
    tools=(
        ToolSpec(
            server="web",
            name="search",
            description="Search the web using DuckDuckGo. Use this when you need current information, news, or anything you do not know yet.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    }
                },
                "required": ["query"],
            },
            handler=_handle_web_search,
            exposures=frozenset({"chat", "proactive"}),
            legacy_names=("web_search",),
        ),
        ToolSpec(
            server="web",
            name="fetch_url",
            description="Fetch the content of a web page and return it as plain text. Use this to read articles, documentation, and linked pages.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch",
                    }
                },
                "required": ["url"],
            },
            handler=_handle_fetch_url,
            exposures=frozenset({"chat", "proactive"}),
            legacy_names=("fetch_url",),
        ),
    ),
)
