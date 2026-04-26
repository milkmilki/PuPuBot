"""Prompt formatting helpers for important events."""

from __future__ import annotations


def format_important_events_section(
    important_events: list[dict] | None,
    *,
    heading: str = "## 你记得并在意的事",
) -> str:
    items = important_events or []
    if not items:
        return ""

    lines = [heading]
    for event in items[:8]:
        title = str(event.get("title") or "未命名事件").strip()
        event_time = str(event.get("event_time") or "").strip()
        time_text = str(event.get("time_text") or "").strip()
        details = str(event.get("details") or "").strip()
        followup_hint = str(event.get("followup_hint") or "").strip()
        linked_task_id = event.get("linked_task_id")

        parts = [title]
        if event_time:
            parts.append(event_time)
        elif time_text:
            parts.append(f"线索:{time_text}")
        if details:
            parts.append(details)
        if followup_hint and followup_hint != details:
            parts.append(f"跟进:{followup_hint}")
        if linked_task_id:
            parts.append("已设提醒")

        lines.append("- " + " | ".join(parts))

    lines.append("自然记着，顺着当前话题接住；不要硬切话题，也不要短时间反复提同一件事。")
    return "\n".join(lines)
