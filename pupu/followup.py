"""Followup protocol and scheduling helpers.

This module owns the dialogue output contract and the wait-followup policy.
It is intentionally separate from the main chat orchestrator in `pupu.agent`.
"""

from __future__ import annotations

import json
import re

WAIT_DELAY_SECONDS = 180

DIALOGUE_OUTPUT_PROTOCOL = (
    "\n\n## 对话输出协议\n"
    "- 你必须只输出一个 JSON 对象，不要输出 Markdown 代码块。\n"
    "- JSON 字段固定为 content 和 should_wait。\n"
    "- content 是你要实际发给用户的话（字符串）。\n"
    "- content 不要带任何说话人前缀，不要写“仆仆：”“璐璐：”“用户：”这类剧本格式。\n"
    "- should_wait 是布尔值：true 表示你期待用户回复；false 表示不期待。\n"
    "- 只要 content 里出现提问、让对方给结论/选择/反馈，should_wait 应优先设为 true。\n"
    "- 除 JSON 对象外不要输出任何其他文字。"
)


def _normalize_should_wait(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "是", "需要", "wait"}


def _infer_should_wait_from_content(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False

    if "?" in text or "？" in text:
        return True

    strong_markers = (
        "给个准话",
        "告诉我",
        "你觉得",
        "你选",
        "要不要",
        "行不行",
        "可以吗",
        "好吗",
        "在吗",
        "回我",
    )
    if any(marker in text for marker in strong_markers):
        return True

    tail = text[-12:]
    return tail.endswith("吗") or tail.endswith("呢")


def _strip_code_fence(raw_text: str) -> str:
    raw = (raw_text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines:
        lines = lines[1:]
    raw = "\n".join(lines)
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _repair_unescaped_quotes_in_json_strings(raw_text: str) -> str:
    if not raw_text:
        return raw_text

    chars: list[str] = []
    in_string = False
    escaped = False
    length = len(raw_text)

    def _next_non_ws(index: int) -> str:
        j = index + 1
        while j < length and raw_text[j].isspace():
            j += 1
        return raw_text[j] if j < length else ""

    for i, ch in enumerate(raw_text):
        if not in_string:
            chars.append(ch)
            if ch == '"':
                in_string = True
            continue

        if escaped:
            chars.append(ch)
            escaped = False
            continue

        if ch == "\\":
            chars.append(ch)
            escaped = True
            continue

        if ch == '"':
            next_ch = _next_non_ws(i)
            if next_ch and next_ch not in {",", "}", "]", ":"}:
                chars.append('\\"')
                continue
            chars.append(ch)
            in_string = False
            continue

        chars.append(ch)

    return "".join(chars)


def _parse_dialogue_output(raw_text: str) -> tuple[str, bool]:
    cleaned = _strip_code_fence(raw_text)
    decoder = json.JSONDecoder()
    candidates = []
    if cleaned:
        candidates.append(cleaned)
        brace_index = cleaned.find("{")
        if brace_index != -1:
            candidates.append(cleaned[brace_index:])

    seen = set()
    for candidate in candidates:
        variants = [
            candidate,
            re.sub(r",\s*([}\]])", r"\1", candidate),
            _repair_unescaped_quotes_in_json_strings(candidate),
            _repair_unescaped_quotes_in_json_strings(
                re.sub(r",\s*([}\]])", r"\1", candidate)
            ),
        ]
        for variant in variants:
            if variant in seen:
                continue
            seen.add(variant)
            try:
                parsed, _ = decoder.raw_decode(variant)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue

            content = str(parsed.get("content", "")).strip()
            if not content:
                content = str(parsed.get("text", "")).strip()
            if not content:
                content = str(raw_text or "").strip()

            if "should_wait" in parsed or "should_wait_for_user" in parsed:
                should_wait = _normalize_should_wait(
                    parsed.get("should_wait", parsed.get("should_wait_for_user", False))
                )
            else:
                should_wait = _infer_should_wait_from_content(content)
            return content, should_wait

    fallback_content = str(raw_text or "").strip()
    return fallback_content, _infer_should_wait_from_content(fallback_content)


