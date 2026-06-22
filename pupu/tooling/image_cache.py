"""Short-lived per-session image URL cache for media tools."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

DEFAULT_RECENT_IMAGE_LIMIT = 8
DEFAULT_RECENT_IMAGE_TTL_SECONDS = 30 * 60


@dataclass(slots=True)
class _RecentImages:
    urls: list[str] = field(default_factory=list)
    updated_at: float = 0.0


_lock = threading.Lock()
_cache: dict[str, _RecentImages] = {}


def _normalize_session_id(session_id: str | None) -> str:
    return str(session_id or "default").strip() or "default"


def _normalize_urls(image_urls: list[str] | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in image_urls or []:
        url = str(item or "").strip()
        if not url or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


def remember_recent_images(
    session_id: str | None,
    image_urls: list[str] | None,
    *,
    limit: int = DEFAULT_RECENT_IMAGE_LIMIT,
) -> list[str]:
    urls = _normalize_urls(image_urls)
    if not urls:
        return []
    key = _normalize_session_id(session_id)
    with _lock:
        previous = _cache.get(key)
        merged = urls + (previous.urls if previous else [])
        _cache[key] = _RecentImages(
            urls=_normalize_urls(merged)[: max(1, int(limit or DEFAULT_RECENT_IMAGE_LIMIT))],
            updated_at=time.monotonic(),
        )
        return list(_cache[key].urls)


def get_recent_images(
    session_id: str | None,
    *,
    ttl_seconds: float = DEFAULT_RECENT_IMAGE_TTL_SECONDS,
) -> list[str]:
    key = _normalize_session_id(session_id)
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return []
        if time.monotonic() - entry.updated_at > max(1.0, float(ttl_seconds)):
            _cache.pop(key, None)
            return []
        return list(entry.urls)


def resolve_image_context(session_id: str | None, image_urls: list[str] | None) -> list[str]:
    current = _normalize_urls(image_urls)
    if current:
        return remember_recent_images(session_id, current)
    return get_recent_images(session_id)


def clear_recent_images() -> None:
    with _lock:
        _cache.clear()
