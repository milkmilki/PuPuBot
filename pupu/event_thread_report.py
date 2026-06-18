"""User-facing formatting helpers for event-thread memories."""

from __future__ import annotations

from .event_graph_export import format_event_graph_url_report
from .memory import (
    find_related_event_threads,
    get_event_thread_steps,
    get_recent_event_threads,
)


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _format_event_meta(event: dict) -> str:
    event_time = _compact(event.get("event_time"))
    time_text = _compact(event.get("time_text"))
    thread_key = _compact(event.get("thread_key"))
    status = _compact(event.get("status"))
    people_label = _compact(event.get("people_label"))
    linked_task_id = event.get("linked_task_id")
    try:
        confidence = float(event.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0

    meta_parts = [f"confidence={confidence:.2f}"]
    if event_time:
        meta_parts.append(event_time)
    elif time_text:
        meta_parts.append(f"线索:{time_text}")
    if status:
        meta_parts.append(f"status={status}")
    if linked_task_id:
        meta_parts.append(f"task={linked_task_id}")
    if thread_key:
        meta_parts.append(f"key={thread_key}")
    if people_label:
        meta_parts.append(f"people={people_label}")
    return " | ".join(meta_parts)


def _format_event_lines(index: int, event: dict) -> list[str]:
    title = _compact(event.get("title")) or "未命名事件"
    details = _compact(event.get("current_summary") or event.get("details"))
    cause = _compact(event.get("current_cause"))
    reflection = _compact(event.get("current_reflection"))
    followup_hint = _compact(event.get("followup_hint"))

    lines = [f"{index}. {title}", "   " + _format_event_meta(event)]
    if details:
        lines.append(f"   当前: {details}")
    if cause:
        lines.append(f"   触发: {cause}")
    if reflection:
        lines.append(f"   反思: {reflection}")
    if followup_hint and followup_hint != details:
        lines.append(f"   跟进: {followup_hint}")
    return lines


def format_event_thread_detail_report(session_id: str, key: str) -> str:
    key = _compact(key)
    if not key:
        return "用法：/events detail <key>"
    event, steps = get_event_thread_steps(session_id, key)
    if not event:
        return f"没有找到事件线：{key}"

    title = _compact(event.get("title")) or "未命名事件"
    lines = [f"事件线：{title}", _format_event_meta(event)]
    followup_hint = _compact(event.get("followup_hint"))
    if followup_hint:
        lines.append(f"跟进: {followup_hint}")
    if not steps:
        lines.append("暂无进展节点。")
        return "\n".join(lines)

    lines.append(f"进展 {len(steps)} 条")
    for idx, step in enumerate(steps, start=1):
        step_type = _compact(step.get("step_type")) or "user"
        occurred_at = _compact(step.get("occurred_at") or step.get("created_at"))
        summary = _compact(step.get("summary"))
        cause = _compact(step.get("cause"))
        reflection = _compact(step.get("reflection"))
        header = f"{idx}. [{step_type}]"
        if occurred_at:
            header += f" {occurred_at}"
        lines.append(header)
        if summary:
            lines.append(f"   状态: {summary}")
        if cause:
            lines.append(f"   触发: {cause}")
        if reflection:
            lines.append(f"   反思: {reflection}")
    return "\n".join(lines)


def format_event_thread_search_report(session_id: str, query: str, limit: int = 8) -> str:
    query = _compact(query)
    debug = False
    if query.startswith("--debug "):
        debug = True
        query = _compact(query.removeprefix("--debug "))
    elif query == "--debug":
        debug = True
        query = ""
    if not query:
        return "用法：/events search [--debug] <内容>"
    events = find_related_event_threads(session_id, query, limit=limit, debug=debug)
    if not events:
        return f"没有找到相关事件线：{query}"

    lines = [f"相关事件线 {len(events)} 条" + ("（debug）" if debug else "")]
    for idx, event in enumerate(events, start=1):
        score = float(event.get("score") or 0.0)
        lines.extend(_format_event_lines(idx, event))
        lines.append(f"   匹配: score={score:.2f} {_compact(event.get('reason_for_match'))}")
        if debug:
            detail = event.get("match_debug") or {}
            tokens = detail.get("overlap_tokens") or []
            lines.append(
                "   debug: "
                f"total={float(detail.get('total') or score):.3f} "
                f"fts={float(detail.get('fts_score') or 0.0):.3f} "
                f"overlap={float(detail.get('overlap_score') or 0.0):.3f} "
                f"status_bonus={float(detail.get('status_bonus') or 0.0):.3f} "
                f"recent_bonus={float(detail.get('recent_bonus') or 0.0):.3f} "
                f"confidence_bonus={float(detail.get('confidence_bonus') or 0.0):.3f} "
                f"people_bonus={float(detail.get('people_bonus') or 0.0):.3f} "
                f"fts_attempted={bool(detail.get('fts_attempted'))} "
                f"used_fts={bool(detail.get('used_fts_candidate'))}"
            )
            matched_people = detail.get("matched_people") or []
            if matched_people:
                lines.append(f"   debug_people: {', '.join(str(item) for item in matched_people)}")
            if tokens:
                lines.append(f"   debug_tokens: {', '.join(str(token) for token in tokens[:12])}")
    return "\n".join(lines)


def format_event_threads_report(
    session_id: str,
    limit: int | None = None,
    *,
    sync_memu: bool | None = None,
    query: str = "",
) -> str:
    query = _compact(query)
    if query:
        command, _, rest = query.partition(" ")
        command = command.lower()
        if command in {"url", "html", "graph", "web", "page", "图谱", "网页"}:
            return format_event_graph_url_report(session_id)
        if command in {"detail", "details", "show", "查看", "详情"}:
            return format_event_thread_detail_report(session_id, rest)
        if command in {"search", "find", "query", "查找", "搜索"}:
            return format_event_thread_search_report(session_id, rest)
        return format_event_thread_search_report(session_id, query)

    events = get_recent_event_threads(session_id, limit=limit)
    if not events:
        return "当前没有事件线记忆。"

    lines = [f"事件线 {len(events)} 条"]
    for idx, event in enumerate(events, start=1):
        lines.extend(_format_event_lines(idx, event))

    return "\n".join(lines)
