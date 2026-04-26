"""Post-processing helpers for batch review outputs."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .storage import (
    MAX_SCHEDULED_TASKS_PER_SESSION,
    count_scheduled_tasks,
    create_scheduled_task,
    derive_source_event_key,
    find_matching_scheduled_task,
    get_important_event_by_key,
    link_important_event_task,
    upsert_important_events,
)

DATE_ONLY_DEFAULT_KINDS = {"birthday", "anniversary"}
VALID_REPEATS = {"once", "daily", "weekly", "monthly", "yearly", "interval"}
REPEAT_ALIASES = {
    "everyday": "daily",
    "每日": "daily",
    "每天": "daily",
    "每周": "weekly",
    "每月": "monthly",
    "每年": "yearly",
}


def normalize_review_important_events(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        event_time = str(item.get("event_time", "")).strip()
        time_text = str(item.get("time_text", "")).strip()
        details = str(item.get("details", "")).strip()
        followup_hint = str(item.get("followup_hint", "")).strip()
        source_event_key = derive_source_event_key(
            item.get("source_event_key"),
            title=title,
            kind=kind,
            event_time=event_time,
            time_text=time_text,
        )
        if not (title or details or time_text or event_time):
            continue

        try:
            confidence = float(item.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        cleaned.append(
            {
                "source_event_key": source_event_key,
                "title": title or details[:40] or "未命名事件",
                "kind": kind,
                "event_time": event_time,
                "time_text": time_text,
                "details": details,
                "followup_hint": followup_hint,
                "confidence": confidence,
                "status": "active",
            }
        )
    return cleaned


def normalize_review_task_drafts(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue

        should_create = item.get("should_create", False)
        if isinstance(should_create, str):
            should_create = should_create.strip().lower() in {"1", "true", "yes", "y"}
        else:
            should_create = bool(should_create)

        title = str(item.get("title", "")).strip() or "提醒"
        instruction = str(item.get("instruction", "")).strip()
        run_at = str(item.get("run_at", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        repeat = str(item.get("repeat", "once")).strip().lower()
        repeat = REPEAT_ALIASES.get(repeat, repeat)
        if repeat not in VALID_REPEATS:
            repeat = "once"

        try:
            interval_seconds = int(item.get("interval_seconds"))
        except Exception:
            interval_seconds = None

        source_event_key = derive_source_event_key(
            item.get("source_event_key"),
            title=title,
            kind=kind,
            event_time=run_at,
        )
        cleaned.append(
            {
                "source_event_key": source_event_key,
                "should_create": should_create,
                "title": title,
                "instruction": instruction,
                "run_at": run_at,
                "repeat": repeat,
                "interval_seconds": interval_seconds,
                "kind": kind,
            }
        )
    return cleaned


def save_review_important_events(session_id: str, important_events: list[dict]) -> dict[str, dict]:
    rows = upsert_important_events(session_id, important_events)
    return {str(row["source_event_key"]): row for row in rows}


def apply_review_task_drafts(
    session_id: str,
    task_drafts: list[dict],
    important_event_rows: dict[str, dict] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    results = []
    current = now or datetime.now()
    event_rows = dict(important_event_rows or {})

    for draft in task_drafts:
        source_event_key = derive_source_event_key(
            draft.get("source_event_key"),
            title=str(draft.get("title", "")),
            kind=str(draft.get("kind", "")),
            event_time=str(draft.get("run_at", "")),
        )
        draft["source_event_key"] = source_event_key

        if not draft.get("should_create"):
            results.append(
                {
                    "source_event_key": source_event_key,
                    "status": "skipped",
                    "reason": "model_declined",
                }
            )
            continue

        event_row = event_rows.get(source_event_key) or get_important_event_by_key(
            session_id, source_event_key
        )
        if event_row is None:
            placeholder_rows = upsert_important_events(
                session_id,
                [
                    {
                        "source_event_key": source_event_key,
                        "title": draft.get("title") or "未命名事件",
                        "kind": draft.get("kind") or "",
                        "event_time": draft.get("run_at") or "",
                        "time_text": "",
                        "details": draft.get("instruction") or "",
                        "followup_hint": draft.get("instruction") or "",
                        "confidence": 0.0,
                        "status": "active",
                    }
                ],
            )
            if placeholder_rows:
                event_row = placeholder_rows[0]
                event_rows[source_event_key] = event_row

        if event_row and event_row.get("linked_task_id"):
            results.append(
                {
                    "source_event_key": source_event_key,
                    "status": "linked_existing",
                    "task_id": int(event_row["linked_task_id"]),
                    "reason": "already_linked",
                }
            )
            continue

        run_at, error = _normalize_task_run_at(draft, event_row, current)
        if error:
            results.append(
                {
                    "source_event_key": source_event_key,
                    "status": "skipped",
                    "reason": error,
                }
            )
            continue

        repeat = str(draft.get("repeat") or "once").strip().lower()
        repeat = REPEAT_ALIASES.get(repeat, repeat)
        interval_seconds = draft.get("interval_seconds")
        if repeat == "interval":
            if interval_seconds is None or interval_seconds < 60 or interval_seconds > 604800:
                results.append(
                    {
                        "source_event_key": source_event_key,
                        "status": "skipped",
                        "reason": "invalid_interval_seconds",
                    }
                )
                continue
        else:
            interval_seconds = None

        if count_scheduled_tasks(session_id) >= MAX_SCHEDULED_TASKS_PER_SESSION:
            results.append(
                {
                    "source_event_key": source_event_key,
                    "status": "skipped",
                    "reason": "task_limit_reached",
                }
            )
            continue

        title = str(draft.get("title") or "提醒").strip()[:80] or "提醒"
        instruction = str(draft.get("instruction") or "").strip()
        if not instruction:
            results.append(
                {
                    "source_event_key": source_event_key,
                    "status": "skipped",
                    "reason": "missing_instruction",
                }
            )
            continue

        existing = find_matching_scheduled_task(
            session_id,
            title,
            instruction,
            run_at,
            repeat,
            interval_seconds,
        )
        if existing:
            link_important_event_task(session_id, source_event_key, int(existing["id"]))
            results.append(
                {
                    "source_event_key": source_event_key,
                    "status": "linked_existing",
                    "task_id": int(existing["id"]),
                    "reason": "matched_existing_task",
                }
            )
            continue

        task_id = create_scheduled_task(
            session_id,
            title,
            instruction,
            run_at,
            repeat,
            interval_seconds,
        )
        link_important_event_task(session_id, source_event_key, task_id)
        results.append(
            {
                "source_event_key": source_event_key,
                "status": "created",
                "task_id": task_id,
                "run_at": run_at,
            }
        )

    return results


def _normalize_task_run_at(
    draft: dict,
    event_row: dict | None,
    now: datetime,
) -> tuple[str | None, str | None]:
    raw = str(draft.get("run_at") or "").strip()
    event_kind = str(draft.get("kind") or (event_row or {}).get("kind") or "").strip().lower()
    event_time = str((event_row or {}).get("event_time") or "").strip()

    value = raw or event_time
    if not value:
        return None, "missing_run_at"

    if len(value) == 10:
        if event_kind not in DATE_ONLY_DEFAULT_KINDS:
            return None, "date_only_without_supported_kind"
        try:
            target_date = datetime.fromisoformat(value).date()
        except Exception:
            return None, "invalid_run_at"
        default_dt = datetime.combine(target_date, time(hour=9))
        if target_date == now.date() and default_dt < now:
            default_dt = now + timedelta(minutes=5)
        if default_dt < now - timedelta(seconds=90):
            return None, "run_at_in_past"
        return default_dt.isoformat(timespec="seconds"), None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
    except Exception:
        return None, "invalid_run_at"

    if dt < now - timedelta(seconds=90):
        return None, "run_at_in_past"
    return dt.isoformat(timespec="seconds"), None
