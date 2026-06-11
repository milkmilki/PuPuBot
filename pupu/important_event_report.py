"""User-facing formatting helpers for event-thread memories."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .event_graph_export import format_event_graph_url_report
from .llm import MODEL, json_task
from .maintenance.parsing import _parse_json_object
from .memory import (
    derive_source_event_key,
    find_related_event_threads,
    get_event_thread_steps,
    get_data_dir,
    get_legacy_important_events_for_migration,
    get_recent_important_events,
    migrate_legacy_important_events,
    migrate_legacy_important_events_from_plan,
)

MODEL_MIGRATION_PROMPT = """你在帮一个长期陪伴型聊天体把旧 important_events 迁移成“事件图谱”。

目标：
- 不要机械地一条旧事件生成一条事件线。
- 可以把描述同一件持续事件、同一段关系进展、同一项计划/承诺的旧事件合并成一条 event thread。
- 可以在一条 thread 里创建多个 steps，表达这个事件随时间发生的状态变化。
- 如果某条旧事件确实独立，才创建只有一个初始化 step 的 thread。

输出必须是 JSON 对象，不要 Markdown，不要解释：
{
  "threads": [
    {
      "key": "stable-short-key",
      "title": "事件线标题",
      "kind": "promise|milestone|relationship|state|preference|other",
      "status": "active|scheduled|completed|dropped",
      "event_time": "YYYY-MM-DD 或 ISO 时间，可为空",
      "time_text": "原始时间线索，可为空",
      "followup_hint": "以后自然提起或跟进的方式，可为空",
      "merge_hint": "以后归并相似事件时可用的关键词",
      "confidence": 0.0,
      "source_ids": [1, 2],
      "steps": [
        {
          "step_type": "system|user|instance|time",
          "summary": "这一阶段的事件状态",
          "cause": "导致这个状态的原因；迁移时可说明来自哪些旧事件",
          "reflection": "如果是实例推动且旧事件可判断效果，可以写一句自然反思；否则空",
          "occurred_at": "YYYY-MM-DD 或 ISO 时间，可为空"
        }
      ]
    }
  ],
  "notes": "简短说明合并策略"
}

规则：
- key 要稳定、短、可读，优先英文/拼音/数字/连字符；不要使用数据库 id。
- steps 按时间顺序排列。
- step_type=time 表示时间自然推进的推测状态，summary 必须带“可能/推测/大概/也许”。
- 不要编造旧事件里没有的信息；可以压缩、合并、去重。
- 只输出有长期价值的事件线，低价值、重复或已经无意义的旧事件可以不迁移。
"""

VALID_MIGRATION_STATUSES = {"active", "scheduled", "completed", "dropped"}
VALID_MIGRATION_STEP_TYPES = {"time", "user", "instance", "system"}
DEFAULT_EVENT_MIGRATION_BATCH_SIZE = 10
DEFAULT_EVENT_MIGRATION_MAX_TOKENS = 8000
MAX_EVENT_MIGRATION_MAX_TOKENS = 20000


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _format_event_meta(event: dict) -> str:
    event_time = _compact(event.get("event_time"))
    time_text = _compact(event.get("time_text"))
    source_event_key = _compact(event.get("source_event_key"))
    status = _compact(event.get("status"))
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
    if source_event_key:
        meta_parts.append(f"key={source_event_key}")
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
    if not query:
        return "用法：/events search <内容>"
    events = find_related_event_threads(session_id, query, limit=limit)
    if not events:
        return f"没有找到相关事件线：{query}"

    lines = [f"相关事件线 {len(events)} 条"]
    for idx, event in enumerate(events, start=1):
        score = float(event.get("score") or 0.0)
        lines.extend(_format_event_lines(idx, event))
        lines.append(f"   匹配: score={score:.2f} {_compact(event.get('reason_for_match'))}")
    return "\n".join(lines)


def _legacy_event_for_model(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "source_event_key": row.get("source_event_key"),
        "title": row.get("title") or "",
        "kind": row.get("kind") or "",
        "event_time": row.get("event_time") or "",
        "time_text": row.get("time_text") or "",
        "details": row.get("details") or "",
        "followup_hint": row.get("followup_hint") or "",
        "confidence": row.get("confidence") or 0.0,
        "status": row.get("status") or "active",
        "linked_task_id": row.get("linked_task_id"),
        "last_seen_at": row.get("last_seen_at") or "",
        "created_at": row.get("created_at") or "",
    }


def _normalize_model_migration_threads(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    threads: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_steps = item.get("steps")
        if not isinstance(raw_steps, list):
            raw_steps = []
        steps = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            summary = _compact(raw_step.get("summary") or raw_step.get("details"))
            if not summary:
                continue
            step_type = _compact(raw_step.get("step_type")).lower() or "system"
            if step_type not in VALID_MIGRATION_STEP_TYPES:
                step_type = "system"
            if step_type == "time" and not any(marker in summary for marker in ("可能", "推测", "大概", "也许")):
                summary = "推测：" + summary
            steps.append(
                {
                    "step_type": step_type,
                    "summary": summary,
                    "cause": _compact(raw_step.get("cause")),
                    "reflection": _compact(raw_step.get("reflection")),
                    "occurred_at": _compact(raw_step.get("occurred_at") or raw_step.get("event_time")),
                }
            )
        title = _compact(item.get("title"))
        if not title and steps:
            title = steps[0]["summary"][:40]
        if not title:
            continue
        status = _compact(item.get("status")).lower() or "active"
        if status not in VALID_MIGRATION_STATUSES:
            status = "active"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except Exception:
            confidence = 0.0
        threads.append(
            {
                "key": _compact(item.get("key") or item.get("source_event_key") or item.get("thread_key")),
                "title": title,
                "kind": _compact(item.get("kind")).lower(),
                "status": status,
                "event_time": _compact(item.get("event_time")),
                "time_text": _compact(item.get("time_text")),
                "followup_hint": _compact(item.get("followup_hint")),
                "merge_hint": _compact(item.get("merge_hint") or item.get("followup_hint")),
                "confidence": confidence,
                "linked_task_id": item.get("linked_task_id"),
                "steps": steps,
            }
        )
    return threads


def _event_migration_max_tokens() -> int:
    raw = os.environ.get("PUPU_EVENT_MIGRATION_MAX_TOKENS", "").strip()
    if not raw:
        return DEFAULT_EVENT_MIGRATION_MAX_TOKENS
    try:
        return max(2000, min(MAX_EVENT_MIGRATION_MAX_TOKENS, int(raw)))
    except Exception:
        return DEFAULT_EVENT_MIGRATION_MAX_TOKENS


def _event_migration_batch_size() -> int:
    raw = os.environ.get("PUPU_EVENT_MIGRATION_BATCH_SIZE", "").strip()
    if not raw:
        return DEFAULT_EVENT_MIGRATION_BATCH_SIZE
    try:
        return max(3, min(25, int(raw)))
    except Exception:
        return DEFAULT_EVENT_MIGRATION_BATCH_SIZE


def _planned_thread_snapshot(thread: dict) -> dict:
    steps = thread.get("steps")
    if not isinstance(steps, list):
        steps = []
    return {
        "key": thread.get("key") or thread.get("source_event_key") or "",
        "title": thread.get("title") or "",
        "kind": thread.get("kind") or "",
        "status": thread.get("status") or "active",
        "event_time": thread.get("event_time") or "",
        "followup_hint": thread.get("followup_hint") or "",
        "merge_hint": thread.get("merge_hint") or "",
        "step_count": len(steps),
        "current_summary": (steps[-1] or {}).get("summary") if steps else "",
    }


def _write_event_migration_debug_file(name: str, content: str) -> str:
    safe_name = re.sub(r"[^0-9A-Za-z_.-]+", "-", name).strip("-") or "debug"
    debug_dir = Path(get_data_dir()) / "logs" / "event_migration_raw"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_name}.txt"
    path.write_text(content or "", encoding="utf-8")
    return str(path)


def _merge_model_migration_threads(planned: list[dict], new_threads: list[dict]) -> None:
    by_key = {
        derive_source_event_key(
            item.get("key") or item.get("source_event_key") or item.get("thread_key"),
            title=item.get("title") or "",
            kind=item.get("kind") or "",
            event_time=item.get("event_time") or "",
            time_text=item.get("time_text") or "",
        ): item
        for item in planned
    }
    for item in new_threads:
        key = derive_source_event_key(
            item.get("key") or item.get("source_event_key") or item.get("thread_key"),
            title=item.get("title") or "",
            kind=item.get("kind") or "",
            event_time=item.get("event_time") or "",
            time_text=item.get("time_text") or "",
        )
        item["key"] = key
        existing = by_key.get(key)
        if not existing:
            planned.append(item)
            by_key[key] = item
            continue

        existing_steps = existing.setdefault("steps", [])
        if not isinstance(existing_steps, list):
            existing_steps = []
            existing["steps"] = existing_steps
        seen_steps = {
            (_compact(step.get("step_type")), _compact(step.get("summary")), _compact(step.get("cause")))
            for step in existing_steps
            if isinstance(step, dict)
        }
        for step in item.get("steps") or []:
            if not isinstance(step, dict):
                continue
            signature = (
                _compact(step.get("step_type")),
                _compact(step.get("summary")),
                _compact(step.get("cause")),
            )
            if signature not in seen_steps:
                existing_steps.append(step)
                seen_steps.add(signature)

        for field in ("title", "kind", "status", "event_time", "time_text", "followup_hint", "merge_hint"):
            value = _compact(item.get(field))
            if value:
                existing[field] = value
        try:
            existing["confidence"] = max(
                float(existing.get("confidence") or 0.0),
                float(item.get("confidence") or 0.0),
            )
        except Exception:
            pass


def _call_event_migration_batch(
    session_id: str,
    *,
    batch: list[dict],
    batch_number: str,
    total_batches: int,
    planned_threads: list[dict],
) -> tuple[list[dict], str, int]:
    payload = {
        "session_id": session_id,
        "batch": {
            "index": batch_number,
            "total": total_batches,
            "legacy_important_events": batch,
        },
        "existing_threads": [_planned_thread_snapshot(thread) for thread in planned_threads],
        "instructions": (
            "请只处理当前 batch。若当前 batch 的事件应归并到 existing_threads，"
            "请返回同一个 key 并追加 steps；确实无关才创建新的 key。"
        ),
    }
    raw_text = json_task(
        role="maintenance",
        model=MODEL,
        system=MODEL_MIGRATION_PROMPT,
        user_content=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=_event_migration_max_tokens(),
        task_name="event_graph_migration",
    )
    try:
        parsed = _parse_json_object(raw_text)
    except Exception as exc:
        payload_path = _write_event_migration_debug_file(
            f"batch-{batch_number}-payload.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        raw_path = _write_event_migration_debug_file(
            f"batch-{batch_number}-raw.txt",
            raw_text or "",
        )
        preview = (raw_text or "").strip().replace("\n", " ")[:500]
        raise ValueError(
            f"batch {batch_number}/{total_batches} JSON parse failed: {exc}; "
            f"raw={raw_path}; payload={payload_path}; preview={preview!r}"
        ) from exc
    return _normalize_model_migration_threads(parsed.get("threads", [])), _compact(parsed.get("notes")), len(raw_text or "")


def _run_model_legacy_event_migration(session_id: str) -> dict:
    legacy_events = get_legacy_important_events_for_migration(session_id)
    if not legacy_events:
        return {
            "legacy": 0,
            "planned": 0,
            "created": 0,
            "skipped": 0,
            "steps": 0,
            "removed_simple": 0,
            "failed": 0,
            "notes": "",
            "errors": [],
        }

    planned_threads: list[dict] = []
    notes: list[str] = []
    batch_size = _event_migration_batch_size()
    model_events = [_legacy_event_for_model(row) for row in legacy_events]
    total_batches = (len(model_events) + batch_size - 1) // batch_size
    print(
        "[pupu][event-migration] start "
        f"session={session_id} legacy={len(model_events)} batch_size={batch_size} "
        f"batches={total_batches} max_tokens={_event_migration_max_tokens()}",
        flush=True,
    )
    for batch_index in range(0, len(model_events), batch_size):
        batch = model_events[batch_index : batch_index + batch_size]
        batch_number = batch_index // batch_size + 1
        print(
            "[pupu][event-migration] batch start "
            f"{batch_number}/{total_batches} legacy={len(batch)} "
            f"existing_threads={len(planned_threads)}",
            flush=True,
        )
        try:
            threads, note, raw_chars = _call_event_migration_batch(
                session_id,
                batch=batch,
                batch_number=str(batch_number),
                total_batches=total_batches,
                planned_threads=planned_threads,
            )
        except Exception as exc:
            if len(batch) <= 1:
                raise
            print(
                "[pupu][event-migration] batch retry split "
                f"{batch_number}/{total_batches} reason={type(exc).__name__}: {exc}",
                flush=True,
            )
            threads = []
            note = ""
            raw_chars = 0
            for item_index, item in enumerate(batch, start=1):
                single_label = f"{batch_number}.{item_index}"
                try:
                    single_threads, single_note, single_chars = _call_event_migration_batch(
                        session_id,
                        batch=[item],
                        batch_number=single_label,
                        total_batches=total_batches,
                        planned_threads=planned_threads,
                    )
                except Exception as single_exc:
                    source_key = item.get("source_event_key") or item.get("title") or item.get("id")
                    raise ValueError(
                        f"single event migration failed source={source_key!r}: {single_exc}"
                    ) from single_exc
                _merge_model_migration_threads(planned_threads, single_threads)
                if single_note:
                    notes.append(single_note)
                raw_chars += single_chars
                print(
                    "[pupu][event-migration] single retry done "
                    f"{single_label}/{total_batches} raw_chars={single_chars} "
                    f"threads={len(single_threads)} planned_threads={len(planned_threads)}",
                    flush=True,
                )
            print(
                "[pupu][event-migration] batch done "
                f"{batch_number}/{total_batches} raw_chars={raw_chars} "
                f"batch_threads=split planned_threads={len(planned_threads)}",
                flush=True,
            )
            continue
        _merge_model_migration_threads(planned_threads, threads)
        if note:
            notes.append(note)
        print(
            "[pupu][event-migration] batch done "
            f"{batch_number}/{total_batches} raw_chars={raw_chars} "
            f"batch_threads={len(threads)} planned_threads={len(planned_threads)}",
            flush=True,
        )

    if not planned_threads:
        raise ValueError("model returned no migration threads")
    result = migrate_legacy_important_events_from_plan(session_id, planned_threads)
    result["notes"] = " | ".join(notes[:6])
    result["batches"] = (len(model_events) + batch_size - 1) // batch_size
    return result


def format_event_thread_migration_report(session_id: str, mode: str = "") -> str:
    mode = _compact(mode).lower()
    if mode in {"simple", "raw", "mechanical", "legacy", "fallback", "简单", "机械"}:
        return _format_simple_event_thread_migration_report(session_id)

    try:
        result = _run_model_legacy_event_migration(session_id)
    except Exception as exc:
        return (
            "事件图谱模型迁移失败，未写入新图谱。\n"
            f"error={type(exc).__name__}: {exc}\n"
            "可以修正模型配置后重试 /events migrate；如果只是想机械搬运旧表，可用 /events migrate simple。"
        )

    lines = [
        "事件图谱模型迁移完成。",
        f"legacy={int(result.get('legacy') or 0)}",
        f"planned_threads={int(result.get('planned') or 0)}",
        f"created={int(result.get('created') or 0)}",
        f"steps={int(result.get('steps') or 0)}",
        f"skipped={int(result.get('skipped') or 0)}",
        f"removed_simple={int(result.get('removed_simple') or 0)}",
        f"batches={int(result.get('batches') or 0)}",
        f"max_tokens_per_batch={_event_migration_max_tokens()}",
        f"failed={int(result.get('failed') or 0)}",
        "旧 important_events 表未删除；新读写路径会使用 event_threads/event_steps。",
    ]
    notes = _compact(result.get("notes"))
    if notes:
        lines.append(f"notes={notes}")
    errors = result.get("errors") or []
    if errors:
        lines.append("错误预览:")
        lines.extend(f"- {error}" for error in errors[:5])
    return "\n".join(lines)


def _format_simple_event_thread_migration_report(session_id: str) -> str:
    result = migrate_legacy_important_events(session_id)
    lines = [
        "事件图谱简单迁移完成。",
        f"legacy={int(result.get('legacy') or 0)}",
        f"created={int(result.get('created') or 0)}",
        f"skipped={int(result.get('skipped') or 0)}",
        f"failed={int(result.get('failed') or 0)}",
        "旧 important_events 表未删除；新读写路径会使用 event_threads/event_steps。",
    ]
    errors = result.get("errors") or []
    if errors:
        lines.append("错误预览:")
        lines.extend(f"- {error}" for error in errors[:5])
    return "\n".join(lines)


def format_important_events_report(
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
        if command in {"migrate", "migration", "import", "convert", "迁移", "导入"}:
            return format_event_thread_migration_report(session_id, rest)
        if command in {"url", "html", "graph", "web", "page", "图谱", "网页"}:
            return format_event_graph_url_report(session_id)
        if command in {"detail", "details", "show", "查看", "详情"}:
            return format_event_thread_detail_report(session_id, rest)
        if command in {"search", "find", "query", "查找", "搜索"}:
            return format_event_thread_search_report(session_id, rest)
        return format_event_thread_search_report(session_id, query)

    events = get_recent_important_events(session_id, limit=limit)
    if not events:
        return "当前没有事件线记忆。"

    lines = [f"事件线 {len(events)} 条"]
    for idx, event in enumerate(events, start=1):
        lines.extend(_format_event_lines(idx, event))

    return "\n".join(lines)
