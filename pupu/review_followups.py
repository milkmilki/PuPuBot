"""Post-processing helpers for batch review outputs."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .storage import (
    MAX_SCHEDULED_TASKS_PER_SESSION,
    cancel_matching_scheduled_tasks,
    count_scheduled_tasks,
    create_scheduled_task,
    derive_source_event_key,
    find_matching_scheduled_task,
    get_important_event_by_key,
    link_important_event_task,
    reschedule_matching_scheduled_tasks,
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


def normalize_review_important_events(value, *, now: datetime | None = None) -> list[dict]:
    if not isinstance(value, list):
        return []

    now = now or datetime.now()
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
        event_date = _parse_event_date(event_time) or _relative_date_from_text(
            " ".join(part for part in (time_text, title, details, followup_hint) if part),
            now,
        )
        if not event_time and event_date:
            event_time = event_date.isoformat()
        title = _replace_relative_dates(title, now, event_date)
        time_text = _replace_relative_dates(time_text, now, event_date)
        details = _replace_relative_dates(details, now, event_date)
        followup_hint = _replace_relative_dates(followup_hint, now, event_date)
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
        thread_key = str(item.get("thread_key") or item.get("source_event_key") or "").strip()
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
        source_event_key = derive_source_event_key(
            thread_key,
            title=title or summary,
            kind=kind,
            event_time=event_time,
            time_text=time_text,
        )
        cleaned.append(
            {
                "action": action,
                "thread_key": source_event_key,
                "source_event_key": source_event_key,
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
                "source_event_key": str(item.get("source_event_key", "")).strip(),
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


def save_review_important_events(
    identity_session: str,
    important_events: list[dict],
    *,
    context_session: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
) -> dict[str, dict]:
    rows = upsert_important_events(
        identity_session,
        _with_review_source_range(
            important_events,
            context_session=context_session,
            source_msg_start_id=source_msg_start_id,
            source_msg_end_id=source_msg_end_id,
        ),
    )
    return {str(row["source_event_key"]): row for row in rows}


def save_review_event_updates(
    identity_session: str,
    event_updates: list[dict],
    *,
    context_session: str | None = None,
    source_msg_start_id: int | None = None,
    source_msg_end_id: int | None = None,
) -> dict[str, dict]:
    rows = upsert_important_events(
        identity_session,
        _with_review_source_range(
            event_updates,
            context_session=context_session,
            source_msg_start_id=source_msg_start_id,
            source_msg_end_id=source_msg_end_id,
        ),
    )
    return {str(row["source_event_key"]): row for row in rows}


def apply_review_task_drafts(
    session_id: str,
    task_drafts: list[dict],
    important_event_rows: dict[str, dict] | None = None,
    now: datetime | None = None,
    *,
    identity_session: str | None = None,
) -> list[dict]:
    results = []
    identity_session = str(identity_session or session_id)
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
            identity_session, source_event_key
        )
        if event_row is None:
            placeholder_rows = upsert_important_events(
                identity_session,
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
            link_important_event_task(
                identity_session,
                source_event_key,
                int(existing["id"]),
            )
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
        link_important_event_task(identity_session, source_event_key, task_id)
        results.append(
            {
                "source_event_key": source_event_key,
                "status": "created",
                "task_id": task_id,
                "run_at": run_at,
            }
        )

    return results


def apply_review_task_updates(
    session_id: str,
    task_updates: list[dict],
    important_event_rows: dict[str, dict] | None = None,
    now: datetime | None = None,
    *,
    identity_session: str | None = None,
) -> list[dict]:
    results = []
    identity_session = str(identity_session or session_id)
    event_rows = dict(important_event_rows or {})
    current = now or datetime.now()
    for update in task_updates:
        action = str(update.get("action", "")).strip().lower()
        query = str(update.get("query", "")).strip()
        if action == "create":
            draft = {
                "source_event_key": update.get("source_event_key"),
                "should_create": True,
                "title": update.get("title") or "提醒",
                "instruction": update.get("instruction") or "",
                "run_at": update.get("run_at") or "",
                "repeat": update.get("repeat") or "once",
                "interval_seconds": update.get("interval_seconds"),
                "kind": update.get("kind") or "",
            }
            draft_results = apply_review_task_drafts(
                session_id,
                [draft],
                event_rows,
                current,
                identity_session=identity_session,
            )
            item = dict(draft_results[0] if draft_results else {})
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
