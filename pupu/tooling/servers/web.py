"""Web/search tool server."""

from __future__ import annotations

import os
import warnings
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS as ModernDDGS  # type: ignore[import-not-found]
except ImportError:
    ModernDDGS = None

try:
    from duckduckgo_search import DDGS as LegacyDDGS
except ImportError:
    LegacyDDGS = None

from ..base import BuiltinToolServer, ToolContext, ToolSpec

SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
DEFAULT_SEARCH_CHAIN = ("tavily", "ddg_html")
PROVIDER_ALIASES = {
    "duckduckgo": "ddg_html",
    "duckduckgo_html": "ddg_html",
    "ddg": "ddg_html",
    "bing": "bing_html",
    "bing_html_page": "bing_html",
    "legacy": "legacy_ddgs",
    "duckduckgo_search": "legacy_ddgs",
    "modern_ddgs": "ddgs",
}


def _format_search_results(results: list[dict[str, str]]) -> str:
    if not results:
        return "没搜到相关结果"

    output = []
    for result in results:
        title = result.get("title", "").strip() or "(no title)"
        href = result.get("href", "").strip() or "(no url)"
        body = result.get("body", "").strip() or "(no snippet)"
        output.append(f"**{title}**\n{href}\n{body}\n")
    return "\n".join(output)


def _normalize_results(results: list[dict[str, str]], max_results: int) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in results:
        href = (result.get("href") or "").strip()
        title = (result.get("title") or "").strip()
        body = (result.get("body") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        normalized.append({"title": title, "href": href, "body": body})
        if len(normalized) >= max_results:
            break
    return normalized


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _normalize_provider_name(name: str) -> str:
    value = str(name or "").strip().lower().replace("-", "_")
    return PROVIDER_ALIASES.get(value, value)


def _search_provider_chain() -> list[str]:
    primary = _normalize_provider_name(os.environ.get("PUPU_WEB_SEARCH_PROVIDER", ""))
    fallbacks = [_normalize_provider_name(name) for name in _env_list("PUPU_WEB_SEARCH_FALLBACKS")]

    configured = [name for name in [primary, *fallbacks] if name]
    if not configured:
        configured = list(DEFAULT_SEARCH_CHAIN)

    chain: list[str] = []
    seen: set[str] = set()
    for name in configured:
        if name in seen:
            continue
        seen.add(name)
        chain.append(name)
    return chain


def _search_with_tavily(query: str, max_results: int) -> tuple[list[dict[str, str]], str]:
    api_key = (
        os.environ.get("PUPU_TAVILY_API_KEY", "").strip()
        or os.environ.get("TAVILY_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError("Tavily API key is not configured (PUPU_TAVILY_API_KEY)")

    payload: dict[str, object] = {
        "query": query,
        "max_results": max_results,
        "search_depth": os.environ.get("PUPU_TAVILY_SEARCH_DEPTH", "basic").strip() or "basic",
        "topic": os.environ.get("PUPU_TAVILY_TOPIC", "general").strip() or "general",
        "include_answer": False,
        "include_raw_content": False,
    }
    raw_days = os.environ.get("PUPU_TAVILY_DAYS", "").strip()
    if raw_days:
        try:
            payload["days"] = max(1, int(raw_days))
        except ValueError:
            print(f"[pupu][search] ignore invalid PUPU_TAVILY_DAYS={raw_days!r}")
    time_range = os.environ.get("PUPU_TAVILY_TIME_RANGE", "").strip()
    if time_range:
        payload["time_range"] = time_range

    resp = httpx.post(
        "https://api.tavily.com/search",
        headers={
            **SEARCH_HEADERS,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        raise RuntimeError("Tavily response did not contain a results list")

    results: list[dict[str, str]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        href = str(item.get("url") or item.get("href") or "").strip()
        body = str(item.get("content") or item.get("snippet") or "").strip()
        published = str(item.get("published_date") or item.get("published_at") or "").strip()
        if published and published not in body:
            body = f"{body}\n发布时间: {published}" if body else f"发布时间: {published}"
        results.append({"title": title, "href": href, "body": body})

    normalized = _normalize_results(results, max_results)
    if results and not normalized:
        raise RuntimeError("Tavily returned results without URLs")
    return normalized, "tavily"


def _search_with_modern_ddgs(query: str, max_results: int) -> tuple[list[dict[str, str]], str]:
    if ModernDDGS is None:
        raise RuntimeError("ddgs is not installed")

    with ModernDDGS(timeout=15) as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    return _normalize_results(results, max_results), "ddgs"


def _search_with_duckduckgo_html(query: str, max_results: int) -> tuple[list[dict[str, str]], str]:
    resp = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "kl": "cn-zh"},
        headers=SEARCH_HEADERS,
        follow_redirects=True,
        timeout=15,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[dict[str, str]] = []
    for node in soup.select(".result"):
        link = node.select_one(".result__title a, a.result__a")
        if not link:
            continue
        snippet = node.select_one(".result__snippet")
        href = (link.get("href") or "").strip()
        title = link.get_text(" ", strip=True)
        body = snippet.get_text(" ", strip=True) if snippet else ""
        results.append({"title": title, "href": href, "body": body})

    results = _normalize_results(results, max_results)
    if results:
        return results, "duckduckgo_html_page"
    raise RuntimeError("duckduckgo html page returned no parsable results")


def _search_with_bing_html(query: str, max_results: int) -> tuple[list[dict[str, str]], str]:
    url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=zh-Hans"
    resp = httpx.get(
        url,
        headers=SEARCH_HEADERS,
        follow_redirects=True,
        timeout=15,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[dict[str, str]] = []
    for node in soup.select("li.b_algo"):
        link = node.select_one("h2 a")
        if not link:
            continue
        snippet = node.select_one(".b_caption p, .b_snippet, p")
        href = (link.get("href") or "").strip()
        title = link.get_text(" ", strip=True)
        body = snippet.get_text(" ", strip=True) if snippet else ""
        results.append({"title": title, "href": href, "body": body})

    results = _normalize_results(results, max_results)
    if results:
        return results, "bing_html_page"
    raise RuntimeError("bing html page returned no parsable results")


def _search_with_legacy_ddgs(query: str, max_results: int) -> tuple[list[dict[str, str]], str]:
    if LegacyDDGS is None:
        raise RuntimeError("duckduckgo_search is not installed")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with LegacyDDGS(timeout=15) as ddgs:
            attempts: list[tuple[str, object]] = []
            if hasattr(ddgs, "_text_html"):
                attempts.append(
                    (
                        "duckduckgo_html_internal",
                        lambda: ddgs._text_html(query, None, None, max_results),
                    )
                )
            if hasattr(ddgs, "_text_lite"):
                attempts.append(
                    (
                        "duckduckgo_lite_internal",
                        lambda: ddgs._text_lite(query, None, None, max_results),
                    )
                )
            attempts.append(
                (
                    "legacy_text_api",
                    lambda: ddgs.text(query, max_results=max_results),
                )
            )

            last_exc: Exception | None = None
            for backend_name, runner in attempts:
                try:
                    results = _normalize_results(list(runner() or []), max_results)
                    if results:
                        return results, backend_name
                except Exception as exc:
                    print(f"[pupu][search] backend={backend_name} failed error={exc}")
                    last_exc = exc

    if last_exc is not None:
        raise last_exc
    return [], "legacy_empty"


def web_search(query: str, max_results: int = 5) -> str:
    try:
        query = (query or "").strip()
        if not query:
            return "搜索词不能为空"

        provider_map = {
            "tavily": _search_with_tavily,
            "ddgs": _search_with_modern_ddgs,
            "ddg_html": _search_with_duckduckgo_html,
            "bing_html": _search_with_bing_html,
            "legacy_ddgs": _search_with_legacy_ddgs,
        }
        chain = _search_provider_chain()

        errors: list[str] = []
        for provider_name in chain:
            backend = provider_map.get(provider_name)
            if backend is None:
                errors.append(f"{provider_name}: unknown provider")
                print(f"[pupu][search] provider={provider_name} skipped reason=unknown")
                continue
            try:
                results, backend_name = backend(query, max_results)
                print(
                    f"[pupu][search] provider={provider_name} backend={backend_name} "
                    f"query={query!r} results={len(results)}"
                )
                return _format_search_results(results)
            except Exception as exc:
                errors.append(f"{provider_name}: {exc}")
                print(
                    f"[pupu][search] provider={provider_name} query={query!r} failed error={exc}"
                )

        return "搜索出错了：" + " | ".join(errors[:3])
    except Exception as exc:
        print(f"[pupu][search] failed query={query!r} error={exc}")
        return f"搜索出错了：{exc}"


def fetch_url(url: str) -> str:
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=15, headers=SEARCH_HEADERS)
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
    max_results = tool_input.get("max_results", 5)
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5
    return web_search(tool_input["query"], max_results=max(1, min(max_results, 10)))


def _handle_fetch_url(tool_input: dict, _context: ToolContext) -> str:
    return fetch_url(tool_input["url"])


WEB_SERVER = BuiltinToolServer(
    name="web",
    description="Web search and page fetch tools.",
    tools=(
        ToolSpec(
            server="web",
            name="search",
            description="Search the web using the configured provider chain. Use this when you need current information, news, or anything you do not know yet.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (1-10). Defaults to 5.",
                    },
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
