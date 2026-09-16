"""Short-lived image context shared by chat surfaces and native vision providers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .instance_context import get_current_instance_context

DEFAULT_RECENT_IMAGE_LIMIT = 8
DEFAULT_RECENT_IMAGE_TTL_SECONDS = 30 * 60
DEFAULT_IMAGE_CACHE_MAX_SESSIONS = 64
DEFAULT_IMAGE_CACHE_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_SESSION_IMAGE_CACHE_MAX_BYTES = 24 * 1024 * 1024

_IMAGE_REFERENCE_MARKERS = (
    "刚才那张",
    "刚刚那张",
    "上一张",
    "这张图",
    "那张图",
    "图里",
    "图中",
    "画里",
    "上面写",
    "刚才的照片",
    "刚才的截图",
)


@dataclass(slots=True)
class _RecentImages:
    urls: list[str] = field(default_factory=list)
    images: dict[str, tuple[str, str]] = field(default_factory=dict)
    updated_at: float = 0.0


_lock = threading.Lock()
_cache: dict[str, _RecentImages] = {}


def _image_size(image: tuple[str, str]) -> int:
    return len(image[0].encode("ascii", errors="ignore"))


def _entry_size(entry: _RecentImages) -> int:
    return sum(_image_size(image) for image in entry.images.values())


def _prune_locked(now: float) -> None:
    expired = [
        key
        for key, entry in _cache.items()
        if now - entry.updated_at > DEFAULT_RECENT_IMAGE_TTL_SECONDS
    ]
    for key in expired:
        _cache.pop(key, None)

    while len(_cache) > DEFAULT_IMAGE_CACHE_MAX_SESSIONS:
        oldest_key = min(_cache, key=lambda key: _cache[key].updated_at)
        _cache.pop(oldest_key, None)

    total_bytes = sum(_entry_size(entry) for entry in _cache.values())
    while _cache and total_bytes > DEFAULT_IMAGE_CACHE_MAX_BYTES:
        oldest_key = min(_cache, key=lambda key: _cache[key].updated_at)
        total_bytes -= _entry_size(_cache[oldest_key])
        _cache.pop(oldest_key, None)


def _trim_entry_images(entry: _RecentImages) -> None:
    while entry.images and _entry_size(entry) > DEFAULT_SESSION_IMAGE_CACHE_MAX_BYTES:
        oldest_cached_url = next(
            (url for url in reversed(entry.urls) if url in entry.images),
            None,
        )
        if oldest_cached_url is None:
            break
        entry.images.pop(oldest_cached_url, None)


def _session_key(session_id: str | None) -> str:
    session = str(session_id or "default").strip() or "default"
    context = get_current_instance_context()
    instance_id = str(context.instance_id).strip() if context is not None else "global"
    return f"{instance_id}\0{session}"


def _normalize_urls(image_urls: list[str] | None) -> list[str]:
    urls: list[str] = []
    for item in image_urls or []:
        url = str(item or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def remember_recent_images(session_id: str | None, image_urls: list[str] | None) -> list[str]:
    current = _normalize_urls(image_urls)
    if not current:
        return []
    key = _session_key(session_id)
    with _lock:
        now = time.monotonic()
        _prune_locked(now)
        previous = _cache.get(key)
        merged = _normalize_urls(current + (previous.urls if previous else []))[
            :DEFAULT_RECENT_IMAGE_LIMIT
        ]
        previous_images = previous.images if previous else {}
        _cache[key] = _RecentImages(
            urls=merged,
            images={url: previous_images[url] for url in merged if url in previous_images},
            updated_at=now,
        )
        _prune_locked(now)
    return current


def get_recent_images(
    session_id: str | None,
    *,
    ttl_seconds: float = DEFAULT_RECENT_IMAGE_TTL_SECONDS,
) -> list[str]:
    key = _session_key(session_id)
    with _lock:
        now = time.monotonic()
        _prune_locked(now)
        entry = _cache.get(key)
        if entry is None:
            return []
        if now - entry.updated_at > max(1.0, float(ttl_seconds)):
            _cache.pop(key, None)
            return []
        entry.updated_at = now
        return list(entry.urls)


def remember_image_data(
    session_id: str | None,
    url: str,
    b64: str,
    media_type: str,
) -> None:
    normalized_url = str(url or "").strip()
    if not normalized_url or not b64:
        return
    key = _session_key(session_id)
    with _lock:
        now = time.monotonic()
        _prune_locked(now)
        previous = _cache.get(key)
        merged = _normalize_urls([normalized_url] + (previous.urls if previous else []))[
            :DEFAULT_RECENT_IMAGE_LIMIT
        ]
        images = dict(previous.images) if previous else {}
        images[normalized_url] = (str(b64), str(media_type or "image/jpeg"))
        entry = _RecentImages(
            urls=merged,
            images={url: images[url] for url in merged if url in images},
            updated_at=now,
        )
        _trim_entry_images(entry)
        _cache[key] = entry
        _prune_locked(now)


def get_image_data(
    session_id: str | None,
    url: str,
    *,
    ttl_seconds: float = DEFAULT_RECENT_IMAGE_TTL_SECONDS,
) -> tuple[str, str] | None:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return None
    key = _session_key(session_id)
    with _lock:
        now = time.monotonic()
        _prune_locked(now)
        entry = _cache.get(key)
        if entry is None:
            return None
        if now - entry.updated_at > max(1.0, float(ttl_seconds)):
            _cache.pop(key, None)
            return None
        entry.updated_at = now
        return entry.images.get(normalized_url)


def resolve_native_image_context(
    session_id: str | None,
    current_image_urls: list[str] | None,
    user_input: str,
) -> list[str]:
    current = remember_recent_images(session_id, current_image_urls)
    if current:
        return current
    text = str(user_input or "").strip()
    if any(marker in text for marker in _IMAGE_REFERENCE_MARKERS):
        return get_recent_images(session_id)
    return []


def clear_recent_images() -> None:
    with _lock:
        _cache.clear()
