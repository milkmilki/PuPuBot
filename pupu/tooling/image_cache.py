"""Short-lived per-session image cache for media tools."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from hashlib import sha1

DEFAULT_RECENT_IMAGE_LIMIT = 8
DEFAULT_RECENT_IMAGE_TTL_SECONDS = 30 * 60


@dataclass(slots=True)
class _RecentImages:
    urls: list[str] = field(default_factory=list)
    images: dict[str, tuple[str, str]] = field(default_factory=dict)
    vision_texts: dict[str, str] = field(default_factory=dict)
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
        normalized = _normalize_urls(merged)[: max(1, int(limit or DEFAULT_RECENT_IMAGE_LIMIT))]
        previous_images = previous.images if previous else {}
        previous_vision_texts = previous.vision_texts if previous else {}
        _cache[key] = _RecentImages(
            urls=normalized,
            images={url: previous_images[url] for url in normalized if url in previous_images},
            vision_texts=dict(previous_vision_texts),
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


def remember_image_data(
    session_id: str | None,
    url: str,
    b64: str,
    media_type: str,
    *,
    limit: int = DEFAULT_RECENT_IMAGE_LIMIT,
) -> None:
    normalized_url = str(url or "").strip()
    if not normalized_url or not b64:
        return
    key = _normalize_session_id(session_id)
    with _lock:
        previous = _cache.get(key)
        merged = [normalized_url] + (previous.urls if previous else [])
        normalized = _normalize_urls(merged)[: max(1, int(limit or DEFAULT_RECENT_IMAGE_LIMIT))]
        images = dict(previous.images) if previous else {}
        vision_texts = dict(previous.vision_texts) if previous else {}
        images[normalized_url] = (str(b64), str(media_type or "image/jpeg"))
        _cache[key] = _RecentImages(
            urls=normalized,
            images={item: images[item] for item in normalized if item in images},
            vision_texts=vision_texts,
            updated_at=time.monotonic(),
        )


def get_image_data(
    session_id: str | None,
    url: str,
    *,
    ttl_seconds: float = DEFAULT_RECENT_IMAGE_TTL_SECONDS,
) -> tuple[str, str] | None:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return None
    key = _normalize_session_id(session_id)
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.updated_at > max(1.0, float(ttl_seconds)):
            _cache.pop(key, None)
            return None
        return entry.images.get(normalized_url)


def _vision_key(url: str, model: str, prompt: str) -> str:
    raw = "\0".join((str(url or ""), str(model or ""), str(prompt or "")))
    return sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def remember_vision_text(
    session_id: str | None,
    url: str,
    model: str,
    prompt: str,
    text: str,
) -> None:
    normalized_url = str(url or "").strip()
    value = str(text or "").strip()
    if not normalized_url or not value:
        return
    key = _normalize_session_id(session_id)
    cache_key = _vision_key(normalized_url, model, prompt)
    with _lock:
        previous = _cache.get(key)
        urls = _normalize_urls([normalized_url] + (previous.urls if previous else []))[:DEFAULT_RECENT_IMAGE_LIMIT]
        images = dict(previous.images) if previous else {}
        vision_texts = dict(previous.vision_texts) if previous else {}
        vision_texts[cache_key] = value
        _cache[key] = _RecentImages(
            urls=urls,
            images={item: images[item] for item in urls if item in images},
            vision_texts=vision_texts,
            updated_at=time.monotonic(),
        )


def get_vision_text(
    session_id: str | None,
    url: str,
    model: str,
    prompt: str,
    *,
    ttl_seconds: float = DEFAULT_RECENT_IMAGE_TTL_SECONDS,
) -> str | None:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return None
    key = _normalize_session_id(session_id)
    cache_key = _vision_key(normalized_url, model, prompt)
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.updated_at > max(1.0, float(ttl_seconds)):
            _cache.pop(key, None)
            return None
        return entry.vision_texts.get(cache_key)


def resolve_image_context(session_id: str | None, image_urls: list[str] | None) -> list[str]:
    current = _normalize_urls(image_urls)
    if current:
        return remember_recent_images(session_id, current)
    return get_recent_images(session_id)


def clear_recent_images() -> None:
    with _lock:
        _cache.clear()
