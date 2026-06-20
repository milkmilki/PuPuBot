"""Deterministic memU cache reconciliation.

SQLite is PuPu's source of truth. memU only stores rebuildable semantic RAG
cards that point back to SQLite rows.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from .memu_adapter import (
    _json_compact,
    _log,
    reconcile_memu_source_cache,
    rebuild_memu_source_cache,
)

_tidy_lock = threading.Lock()


def _busy_result(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "status": "busy",
        "checked": 0,
        "present": 0,
        "missing": 0,
        "created": 0,
        "deleted": 0,
        "refreshed": 0,
        "duplicates": 0,
        "orphaned": 0,
        "failed": 0,
        "error": "",
        "note": "memU cache reconciliation is already running",
    }


def analyze_memu_tidy(
    identity_session: str,
    *,
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    del now, expire_days
    _log(f"source cache analyze start identity={identity_session}")
    return reconcile_memu_source_cache(identity_session, dry_run=True)


def run_memu_tidy(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    del now, expire_days
    normalized_mode = str(mode or "apply").strip().lower()
    if normalized_mode not in {"apply", "check", "rebuild"}:
        raise ValueError("memU tidy mode must be 'apply', 'check', or 'rebuild'")

    if not _tidy_lock.acquire(blocking=False):
        _log(f"source cache skipped status=busy identity={identity_session} mode={normalized_mode}")
        return _busy_result(normalized_mode)

    try:
        if normalized_mode == "check":
            return reconcile_memu_source_cache(identity_session, dry_run=True)
        if normalized_mode == "rebuild":
            return rebuild_memu_source_cache(identity_session)
        return reconcile_memu_source_cache(identity_session, dry_run=False)
    finally:
        _tidy_lock.release()


def _keys_preview(values: object, limit: int = 5) -> str:
    if not isinstance(values, list):
        return ""
    cleaned = [str(item) for item in values if str(item).strip()]
    return ", ".join(cleaned[:limit])


def format_memu_tidy_report(
    result: dict[str, Any],
    *,
    identity_session: str,
    mode: str,
    trigger: str | None = None,
) -> str:
    title = "memU cache check complete" if mode == "check" else "memU cache sync complete"
    if mode == "rebuild":
        title = "memU cache rebuild complete"
    if trigger:
        title = f"{title} ({trigger})"

    lines = [title]
    lines.append(f"- identity: {identity_session}")
    lines.append(f"- status: {result.get('status') or 'unknown'}")
    lines.append(f"- sqlite_sources: {int(result.get('checked') or 0)}")
    lines.append(f"- present: {int(result.get('present') or 0)}")
    lines.append(f"- missing: {int(result.get('missing') or 0)}")
    lines.append(f"- orphaned: {int(result.get('orphaned') or 0)}")
    lines.append(f"- duplicates: {int(result.get('duplicates') or 0)}")
    lines.append(f"- refreshed: {int(result.get('refreshed') or 0)}")
    if mode in {"apply", "rebuild"}:
        lines.append(f"- created: {int(result.get('created') or 0)}")
        lines.append(f"- memu_deleted: {int(result.get('deleted') or 0)}")
        lines.append(f"- failed: {int(result.get('failed') or 0)}")
    if result.get("source_kind_counts"):
        lines.append(f"- sqlite_kinds: {_json_compact(result['source_kind_counts'])}")
    if result.get("memu_kind_counts"):
        lines.append(f"- memu_kinds: {_json_compact(result['memu_kind_counts'])}")
    for key in ("missing_keys", "orphaned_keys", "duplicate_keys", "refreshed_keys"):
        preview = _keys_preview(result.get(key))
        if preview:
            lines.append(f"- {key}: {preview}")
    if result.get("error"):
        lines.append(f"- error: {result['error']}")
    if result.get("note"):
        lines.append(f"- note: {result['note']}")
    return "\n".join(lines)


def run_memu_maintenance(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
    trigger: str = "manual",
) -> str:
    result = run_memu_tidy(identity_session, mode=mode, now=now, expire_days=expire_days)
    return format_memu_tidy_report(
        result,
        identity_session=identity_session,
        mode=mode,
        trigger=trigger,
    )


__all__ = [
    "analyze_memu_tidy",
    "format_memu_tidy_report",
    "run_memu_maintenance",
    "run_memu_tidy",
]
