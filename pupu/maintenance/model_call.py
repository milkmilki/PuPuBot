"""Shared model call for maintenance stages."""

from __future__ import annotations

import json

from ..llm import MODEL, json_task, last_provider_label
from .error_log import record_model_response_error
from .parsing import _parse_json_object


def call_maintenance_model_json(
    system_prompt: str,
    payload: dict,
    max_tokens: int = 5000,
    task_name: str = "maintenance",
) -> dict:
    raw_text = json_task(
        role="maintenance",
        model=MODEL,
        system=system_prompt,
        user_content=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=max_tokens,
        task_name=task_name,
    )
    try:
        return _parse_json_object(raw_text)
    except Exception as exc:
        try:
            log_path = record_model_response_error(
                task_name=task_name,
                provider=last_provider_label("maintenance", MODEL),
                raw_response=raw_text,
                error=exc,
            )
            log_detail = str(log_path)
        except Exception as log_exc:
            log_detail = f"unavailable ({type(log_exc).__name__}: {log_exc})"
        raise ValueError(
            f"{task_name}: unable to parse maintenance response as JSON object; "
            f"raw_response_chars={len(str(raw_text or ''))}; "
            f"raw_response_log={log_detail}"
        ) from exc
