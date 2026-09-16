"""Parsing helpers for model-produced maintenance JSON."""

import json
import re


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


def _parse_json_object(raw_text: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", _strip_code_fence(raw_text), flags=re.DOTALL | re.IGNORECASE)
    decoder = json.JSONDecoder()
    candidates = []
    if cleaned:
        candidates.append(cleaned)
        candidates.extend(cleaned[index:] for index, char in enumerate(cleaned) if char == "{")

    seen = set()
    for candidate in candidates:
        variants = [
            candidate,
            re.sub(r",\s*([}\]])", r"\1", candidate),
        ]
        for variant in variants:
            if variant in seen:
                continue
            seen.add(variant)
            try:
                parsed, _ = decoder.raw_decode(variant)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError("unable to parse maintenance response as JSON object")
