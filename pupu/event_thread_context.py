"""Prompt formatting helpers for event threads."""

from __future__ import annotations


def format_event_threads_section(
    event_threads: list[dict] | None,
    *,
    heading: str = "## 你记得并在意的事",
    subject_label: str | None = None,
    character_name: str | None = None,
) -> str:
    items = event_threads or []
    if not items:
        return ""

    lines = [heading]
    for event in items[:8]:
        def _text(value: object, fallback: str = "") -> str:
            text = str(value or fallback).strip()
            if character_name and character_name != "仆仆":
                text = text.replace("仆仆", character_name)
            return text

        title = _text(event.get("title"), "未命名事件")
        event_time = str(event.get("event_time") or "").strip()
        time_text = _text(event.get("time_text"))
        details = _text(event.get("details"))
        followup_hint = _text(event.get("followup_hint"))
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

        prefix = f"[{subject_label}] " if subject_label else ""
        lines.append("- " + prefix + " | ".join(parts))

    lines.append("自然记着，顺着当前话题接住；不要硬切话题，也不要短时间反复提同一件事。")
    return "\n".join(lines)
