"""OpenAI-compatible embedding client used by PuPu's semantic index."""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import semantic_api_key, semantic_base_url, semantic_model, semantic_timeout

RETRY_ATTEMPTS = 3


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    text = str(exc).lower()
    return any(term in text for term in ("timeout", "timed out", "connection", "temporarily", "eof"))


def _retry_delay_seconds(attempt: int) -> float:
    return min(4.0, float(2 ** max(0, attempt - 1)))


def _extract_embedding(payload: dict[str, Any]) -> list[float]:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return []
    first = data[0]
    if not isinstance(first, dict):
        return []
    embedding = first.get("embedding")
    if not isinstance(embedding, list):
        return []
    return [float(item) for item in embedding]


def embed_text(text: str) -> tuple[list[float], str]:
    api_key = semantic_api_key()
    if not api_key:
        raise RuntimeError("semantic index embedding API key is not configured")
    model = semantic_model()
    payload = {"model": model, "input": str(text or "")}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = httpx.post(
                f"{semantic_base_url()}/embeddings",
                headers=headers,
                json=payload,
                timeout=semantic_timeout(),
            )
            response.raise_for_status()
            embedding = _extract_embedding(response.json())
            if not embedding:
                raise RuntimeError("embedding response did not contain a vector")
            return embedding, model
        except Exception as exc:
            last_error = exc
            if attempt >= RETRY_ATTEMPTS or not _is_transient_error(exc):
                break
            time.sleep(_retry_delay_seconds(attempt))
    raise RuntimeError(f"semantic index embedding failed: {type(last_error).__name__}: {last_error}")


__all__ = ["embed_text"]
