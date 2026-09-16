"""Parsing and normalization helpers for batch review model output."""

from __future__ import annotations

import json
import re

from .review_followups import normalize_review_event_updates, normalize_review_task_updates


def _clean_fact_scalar(value) -> str:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _normalize_fact_updates(value) -> list[dict]:
    raw_items: list[dict] = []
    if isinstance(value, list):
        raw_items.extend(item for item in value if isinstance(item, dict))

    cleaned: list[dict] = []
    for item in raw_items:
        action = _clean_fact_scalar(item.get("action") or "create").lower()
        if action not in {"create", "update_existing"}:
            continue
        confidence = item.get("confidence", 1.0)
        if action == "update_existing":
            try:
                fact_id = int(item.get("fact_id") or item.get("matched_fact_id") or 0)
            except Exception:
                fact_id = 0
            val = _clean_fact_scalar(item.get("value") or item.get("fact_value"))
            if fact_id <= 0 or not val:
                continue
            cleaned.append(
                {
                    "action": "update_existing",
                    "fact_id": fact_id,
                    "value": val,
                    "confidence": confidence,
                }
            )
            continue

        key = _clean_fact_scalar(item.get("key") or item.get("fact_key"))
        val = _clean_fact_scalar(item.get("value") or item.get("fact_value"))
        subject = _clean_fact_scalar(item.get("subject") or item.get("subject_person_key"))
        obj = _clean_fact_scalar(item.get("object") or item.get("object_person_key"))
        scope = (_clean_fact_scalar(item.get("scope") or "person").lower() or "person")
        if not key or not val:
            continue
        cleaned.append(
            {
                "action": "create",
                "subject": subject,
                "object": obj,
                "scope": scope,
                "key": key,
                "value": val,
                "confidence": confidence,
            }
        )
    return cleaned


def _normalize_familiarity_delta(value) -> int:
    try:
        delta = int(value or 0)
    except Exception:
        return 0
    return max(-20, min(20, delta))


def _normalize_batch_review_result(value) -> dict:
    if not isinstance(value, dict):
        return {
            "summary": "",
            "familiarity_delta": 0,
            "fact_updates": [],
            "event_updates": [],
            "task_updates": [],
        }

    event_updates = normalize_review_event_updates(value.get("event_updates", []))
    fact_updates = _normalize_fact_updates(value.get("fact_updates", []))

    return {
        "summary": str(value.get("summary", "")).strip(),
        "familiarity_delta": _normalize_familiarity_delta(
            value.get("familiarity_delta", 0)
        ),
        "fact_updates": fact_updates,
        "event_updates": event_updates,
        "task_updates": normalize_review_task_updates(value.get("task_updates", [])),
    }


def _parse_batch_review_result(raw_text: str) -> dict:
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
            return _normalize_batch_review_result(parsed)

    raise ValueError("unable to parse batch review response as JSON object")


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
