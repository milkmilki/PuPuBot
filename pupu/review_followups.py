"""Post-processing helpers for batch review outputs."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .storage import (
    MAX_SCHEDULED_TASKS_PER_SESSION,
    cancel_matching_scheduled_tasks,
    count_scheduled_tasks,
    create_scheduled_task,
    derive_thread_key,
    find_matching_scheduled_task,
    get_event_thread_by_key,
    link_event_thread_task,
    reschedule_matching_scheduled_tasks,
    upsert_event_threads,
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

ABSOLUTE_DATE_RE = re.compile(r"\d{4}[-年]\d{1,2}(?:[-月]\d{1,2})?")
TIME_OF_DAY_WORDS = ("早上", "上午", "中午", "下午", "晚上", "夜里", "夜晚")


def _date_label(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _parse_event_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _relative_date_from_text(text: str, now: datetime) -> date | None:
    value = str(text or "")
    if any(token in value for token in ("后天",)):
        return now.date() + timedelta(days=2)
    if any(token in value for token in ("明天", "明晚", "明早")):
        return now.date() + timedelta(days=1)
    if any(token in value for token in ("昨天", "昨晚", "昨日")):
        return now.date() - timedelta(days=1)
    if any(token in value for token in ("今天", "今晚", "今夜", "今早", "今日")):
        return now.date()
    return None


def _replace_relative_dates(text: str, now: datetime, event_date: date | None = None) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    today = now.date()
    replacements = [
        ("今天晚上", f"{_date_label(today)}晚上"),
        ("今晚", f"{_date_label(today)}晚上"),
        ("今夜", f"{_date_label(today)}晚上"),
        ("今天早上", f"{_date_label(today)}早上"),
        ("今早", f"{_date_label(today)}早上"),
        ("今天上午", f"{_date_label(today)}上午"),
        ("今天中午", f"{_date_label(today)}中午"),
        ("今天下午", f"{_date_label(today)}下午"),
        ("今天", _date_label(today)),
        ("今日", _date_label(today)),
        ("明天晚上", f"{_date_label(today + timedelta(days=1))}晚上"),
        ("明晚", f"{_date_label(today + timedelta(days=1))}晚上"),
        ("明天早上", f"{_date_label(today + timedelta(days=1))}早上"),
        ("明早", f"{_date_label(today + timedelta(days=1))}早上"),
        ("明天", _date_label(today + timedelta(days=1))),
        ("后天晚上", f"{_date_label(today + timedelta(days=2))}晚上"),
        ("后天", _date_label(today + timedelta(days=2))),
        ("昨天晚上", f"{_date_label(today - timedelta(days=1))}晚上"),
        ("昨晚", f"{_date_label(today - timedelta(days=1))}晚上"),
        ("昨天", _date_label(today - timedelta(days=1))),
    ]
    for needle, replacement in replacements:
        value = value.replace(needle, replacement)
    if event_date and not ABSOLUTE_DATE_RE.search(value) and any(word in value for word in TIME_OF_DAY_WORDS):
        label = _date_label(event_date)
        if value.startswith(TIME_OF_DAY_WORDS):
            return label + value
        return f"{label}，{value}"
    return value


def normalize_review_event_updates(value, *, now: datetime | None = None) -> list[dict]:
    if not isinstance(value, list):
        return []

    now = now or datetime.now()
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        if action not in {"append_step", "create_thread"}:
            continue
        title = str(item.get("title") or "").strip()
        thread_key = str(item.get("thread_key") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        event_time = str(item.get("event_time") or item.get("occurred_at") or "").strip()
        time_text = str(item.get("time_text") or "").strip()
        summary = str(item.get("summary") or item.get("details") or "").strip()
        cause = str(item.get("cause") or "").strip()
        reflection = str(item.get("reflection") or "").strip()
        followup_hint = str(item.get("followup_hint") or "").strip()
        step_type = str(item.get("step_type") or "").strip().lower()
        if step_type not in {"time", "user", "instance", "system"}:
            step_type = "user"
        event_date = _parse_event_date(event_time) or _relative_date_from_text(
            " ".join(part for part in (time_text, title, summary, cause, followup_hint) if part),
            now,
        )
        if not event_time and event_date:
            event_time = event_date.isoformat()
        title = _replace_relative_dates(title, now, event_date)
        time_text = _replace_relative_dates(time_text, now, event_date)
        summary = _replace_relative_dates(summary, now, event_date)
        cause = _replace_relative_dates(cause, now, event_date)
        reflection = _replace_relative_dates(reflection, now, event_date)
        followup_hint = _replace_relative_dates(followup_hint, now, event_date)
        if step_type == "time" and summary and not any(marker in summary for marker in ("可能", "推测", "大概", "也许")):
            summary = "推测：" + summary
        try:
            confidence = float(item.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if not (thread_key or title or summary):
            continue
        thread_key = derive_thread_key(
            thread_key,
            title=title or summary,
            kind=kind,
            event_time=event_time,
            time_text=time_text,
        )
        cleaned.append(
            {
                "action": action,
                "thread_key": thread_key,
                "title": title or summary[:40] or "未命名事件",
                "kind": kind,
                "event_time": event_time,
                "time_text": time_text,
                "summary": summary,
                "details": summary,
                "cause": cause,
                "reflection": reflection,
                "followup_hint": followup_hint,
                "merge_hint": str(item.get("merge_hint") or followup_hint).strip(),
                "step_type": step_type,
                "confidence": confidence,
                "status": str(item.get("status") or "active").strip() or "active",
            }
        )
    return cleaned


def normalize_review_task_updates(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip().lower()
        if action not in {"create", "cancel_matching", "reschedule_matching"}:
            continue
        query = str(item.get("query", "")).strip()
        if action in {"cancel_matching", "reschedule_matching"} and not query:
            continue
        repeat_default = "once" if action == "create" else ""
        repeat = str(item.get("repeat", repeat_default)).strip().lower()
        repeat = REPEAT_ALIASES.get(repeat, repeat)
        if repeat and repeat not in VALID_REPEATS:
            repeat = "once"
        try:
            interval_seconds = int(item.get("interval_seconds"))
        except Exception:
            interval_seconds = None
        cleaned.append(
            {
                "action": action,
                "query": query,
                "thread_key": str(item.get("thread_key", "")).strip(),
                "title": str(item.get("title", "")).strip(),
                "instruction": str(item.get("instruction", "")).strip(),
                "run_at": str(item.get("run_at", "")).strip(),
                "repeat": repeat,
                "interval_seconds": interval_seconds,
                "kind": str(item.get("kind", "")).strip().lower(),
                "reason": str(item.get("reason", "")).strip(),
            }
        )
    return cleaned


def _with_review_source_range(
    items: list[dict],
    *,
    context_session: str | None,
    source_msg_start_id: int | None,
    source_msg_end_id: int | None,
) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if context_session and not row.get("source_context_session"):
            row["source_context_session"] = context_session
        if source_msg_start_id is not None and row.get("source_msg_start_id") is None:
            row["source_msg_start_id"] = source_msg_start_id
        if source_msg_end_id is not None and row.get("source_msg_end_id") is None:
            row["source_msg_end_id"] = source_msg_end_id
        out.append(row)
    return out


def save_review_event_updates(
    identity_session: str,
    event_updates: list[dict],
    *,
    context_session: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
) -> dict[str, dict]:
    rows = upsert_event_threads(
        identity_session,
        _with_review_source_range(
            event_updates,
            context_session=context_session,
            source_msg_start_id=source_msg_start_id,
            source_msg_end_id=source_msg_end_id,
        ),
    )
    return {str(row["thread_key"]): row for row in rows}


def _create_review_task(
    session_id: str,
    update: dict,
    event_rows: dict[str, dict] | None = None,
    now: datetime | None = None,
    *,
    identity_session: str | None = None,
) -> dict:
    identity_session = str(identity_session or session_id)
    current = now or datetime.now()
    event_rows = dict(event_rows or {})
    thread_key = derive_thread_key(
        update.get("thread_key"),
        title=str(update.get("title", "")),
        kind=str(update.get("kind", "")),
        event_time=str(update.get("run_at", "")),
    )

    event_row = event_rows.get(thread_key) or get_event_thread_by_key(
        identity_session, thread_key
    )
    if event_row is None:
        placeholder_rows = upsert_event_threads(
            identity_session,
            [
                {
                    "thread_key": thread_key,
                    "title": update.get("title") or "未命名事件",
                    "kind": update.get("kind") or "",
                    "event_time": update.get("run_at") or "",
                    "time_text": "",
                    "details": update.get("instruction") or "",
                    "followup_hint": update.get("instruction") or "",
                    "confidence": 0.0,
                    "status": "active",
                }
            ],
        )
        if placeholder_rows:
            event_row = placeholder_rows[0]
            event_rows[thread_key] = event_row

    if event_row and event_row.get("linked_task_id"):
        return {
            "thread_key": thread_key,
            "status": "linked_existing",
            "task_id": int(event_row["linked_task_id"]),
            "reason": "already_linked",
        }

    run_at, error = _normalize_task_run_at(update, event_row, current)
    if error:
        return {
            "thread_key": thread_key,
            "status": "skipped",
            "reason": error,
        }

    repeat = str(update.get("repeat") or "once").strip().lower()
    repeat = REPEAT_ALIASES.get(repeat, repeat)
    interval_seconds = update.get("interval_seconds")
    if repeat == "interval":
        if interval_seconds is None or interval_seconds < 60 or interval_seconds > 604800:
            return {
                "thread_key": thread_key,
                "status": "skipped",
                "reason": "invalid_interval_seconds",
            }
    else:
        interval_seconds = None

    if count_scheduled_tasks(session_id) >= MAX_SCHEDULED_TASKS_PER_SESSION:
        return {
            "thread_key": thread_key,
            "status": "skipped",
            "reason": "task_limit_reached",
        }

    title = str(update.get("title") or "提醒").strip()[:80] or "提醒"
    instruction = str(update.get("instruction") or "").strip()
    if not instruction:
        return {
            "thread_key": thread_key,
            "status": "skipped",
            "reason": "missing_instruction",
        }

    existing = find_matching_scheduled_task(
        session_id,
        title,
        instruction,
        run_at,
        repeat,
        interval_seconds,
    )
    if existing:
        link_event_thread_task(
            identity_session,
            thread_key,
            int(existing["id"]),
        )
        return {
            "thread_key": thread_key,
            "status": "linked_existing",
            "task_id": int(existing["id"]),
            "reason": "matched_existing_task",
        }

    task_id = create_scheduled_task(
        session_id,
        title,
        instruction,
        run_at,
        repeat,
        interval_seconds,
    )
    link_event_thread_task(identity_session, thread_key, task_id)
    return {
        "thread_key": thread_key,
        "status": "created",
        "task_id": task_id,
        "run_at": run_at,
    }


def apply_review_task_updates(
    session_id: str,
    task_updates: list[dict],
    event_rows: dict[str, dict] | None = None,
    now: datetime | None = None,
    *,
    identity_session: str | None = None,
) -> list[dict]:
    results = []
    identity_session = str(identity_session or session_id)
    event_rows = dict(event_rows or {})
    current = now or datetime.now()
    for update in task_updates:
        action = str(update.get("action", "")).strip().lower()
        query = str(update.get("query", "")).strip()
        if action == "create":
            item = _create_review_task(
                session_id,
                update,
                event_rows,
                current,
                identity_session=identity_session,
            )
            item = dict(item)
            item["action"] = "create"
            item["query"] = query
            item["reason"] = str(update.get("reason", "")).strip()
            results.append(item)
            continue

        if action == "cancel_matching" and query:
            cancelled = cancel_matching_scheduled_tasks(session_id, query)
            results.append(
                {
                    "action": action,
                    "query": query,
                    "status": "cancelled" if cancelled else "no_match",
                    "task_ids": [int(row["id"]) for row in cancelled],
                    "reason": str(update.get("reason", "")).strip(),
                }
            )
            continue

        if action == "reschedule_matching" and query:
            run_at, error = _normalize_task_run_at(update, None, current)
            if error:
                results.append(
                    {
                        "action": action,
                        "query": query,
                        "status": "skipped",
                        "reason": error,
                    }
                )
                continue

            repeat = str(update.get("repeat") or "").strip().lower()
            repeat = REPEAT_ALIASES.get(repeat, repeat)
            interval_seconds = update.get("interval_seconds")
            if repeat:
                if repeat not in VALID_REPEATS:
                    results.append(
                        {
                            "action": action,
                            "query": query,
                            "status": "skipped",
                            "reason": "invalid_repeat",
                        }
                    )
                    continue
                if repeat == "interval":
                    if interval_seconds is None or interval_seconds < 60 or interval_seconds > 604800:
                        results.append(
                            {
                                "action": action,
                                "query": query,
                                "status": "skipped",
                                "reason": "invalid_interval_seconds",
                            }
                        )
                        continue
                else:
                    interval_seconds = None
            updated = reschedule_matching_scheduled_tasks(
                session_id,
                query,
                run_at,
                repeat or None,
                interval_seconds,
            )
            results.append(
                {
                    "action": action,
                    "query": query,
                    "status": "rescheduled" if updated else "no_match",
                    "task_ids": [int(row["id"]) for row in updated],
                    "run_at": run_at,
                    "reason": str(update.get("reason", "")).strip(),
                }
            )
            continue

        if action or query:
            results.append(
                {
                    "action": action or "unknown",
                    "query": query,
                    "status": "skipped",
                    "reason": "unsupported_or_missing_query",
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
