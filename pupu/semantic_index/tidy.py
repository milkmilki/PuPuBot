"""Deterministic semantic index cache reconciliation."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

from .core import reconcile_source_cache, rebuild_source_cache

_tidy_lock = threading.Lock()


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def analyze_semantic_tidy(
    identity_session: str,
    *,
    now: datetime | None = None,
    expire_days: int = 45,
) -> dict[str, Any]:
    return reconcile_source_cache(identity_session, dry_run=True)


def run_semantic_tidy(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 45,
) -> dict[str, Any]:
    tidy_mode = str(mode or "apply").strip().lower()
    if tidy_mode not in {"apply", "check", "rebuild"}:
        raise ValueError("semantic tidy mode must be 'apply', 'check', or 'rebuild'")
    if not _tidy_lock.acquire(blocking=False):
        return {"status": "busy", "mode": tidy_mode, "note": "semantic index reconciliation is already running"}
    try:
        if tidy_mode == "check":
            return reconcile_source_cache(identity_session, dry_run=True)
        if tidy_mode == "rebuild":
            return rebuild_source_cache(identity_session)
        return reconcile_source_cache(identity_session, dry_run=False)
    finally:
        _tidy_lock.release()


def format_semantic_tidy_report(
    result: dict[str, Any],
    *,
    identity_session: str,
    mode: str,
    trigger: str = "manual",
) -> str:
    title = "semantic index check complete" if mode == "check" else "semantic index sync complete"
    if mode == "rebuild":
        title = "semantic index rebuild complete"
    lines = [f"{title} ({trigger})"]
    for key in (
        "status",
        "checked",
        "present",
        "missing",
        "created",
        "deleted",
        "refreshed",
        "duplicates",
        "orphaned",
        "failed",
    ):
        lines.append(f"- {key}: {result.get(key, 0)}")
    if result.get("source_kind_counts"):
        lines.append(f"- source_kinds: {_json_compact(result['source_kind_counts'])}")
    if result.get("semantic_kind_counts"):
        lines.append(f"- semantic_kinds: {_json_compact(result['semantic_kind_counts'])}")
    if result.get("error"):
        lines.append(f"- error: {result['error']}")
    return "\n".join(lines)


def run_semantic_maintenance(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 45,
) -> str:
    result = run_semantic_tidy(identity_session, mode=mode, now=now, expire_days=expire_days)
    return format_semantic_tidy_report(
        result,
        identity_session=identity_session,
        mode=str(mode or "apply"),
    )


__all__ = [
    "analyze_semantic_tidy",
    "format_semantic_tidy_report",
    "run_semantic_maintenance",
    "run_semantic_tidy",
]
