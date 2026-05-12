"""User-facing formatting helpers for important events."""

from __future__ import annotations

from .memory_index import format_memu_important_events_report
from .memory import get_important_events


def format_important_events_report(session_id: str, limit: int = 12) -> str:
    memu_report = format_memu_important_events_report(session_id)
    if memu_report is not None:
        return memu_report

    events = get_important_events(session_id, limit=limit)
    if not events:
        return "当前没有重要事件记忆。"

    lines = [f"重要事件 {len(events)} 条"]
    for idx, event in enumerate(events, start=1):
        title = str(event.get("title") or "未命名事件").strip()
        event_time = str(event.get("event_time") or "").strip()
        time_text = str(event.get("time_text") or "").strip()
        details = str(event.get("details") or "").strip()
        followup_hint = str(event.get("followup_hint") or "").strip()
        source_event_key = str(event.get("source_event_key") or "").strip()
        status = str(event.get("status") or "").strip()
        linked_task_id = event.get("linked_task_id")
        confidence = float(event.get("confidence") or 0.0)

        meta_parts = [f"confidence={confidence:.2f}"]
        if event_time:
            meta_parts.append(event_time)
        elif time_text:
            meta_parts.append(f"线索:{time_text}")
        if status:
            meta_parts.append(f"status={status}")
        if linked_task_id:
            meta_parts.append(f"task={linked_task_id}")
        if source_event_key:
            meta_parts.append(f"key={source_event_key}")

        lines.append(f"{idx}. {title}")
        lines.append("   " + " | ".join(meta_parts))
        if details:
            lines.append(f"   详情: {details}")
        if followup_hint and followup_hint != details:
            lines.append(f"   跟进: {followup_hint}")

    return "\n".join(lines)
