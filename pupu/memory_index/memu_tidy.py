"""Judge-driven tidy for memU long-term memory."""

from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter
from datetime import datetime
from typing import Any

from ..llm import JUDGE_MODEL, json_task
from .memu_adapter import (
    _delete_legacy_source,
    _json_compact,
    _legacy_source_action,
    _list_items,
    _load_legacy_event_map,
    _log,
    _get_service,
    _norm_text,
    _op_lock,
    _payload_from_item,
    _preview,
    _run,
    is_memu_long_term_enabled,
)
from .memu_tidy_prompt import MEMU_TIDY_JUDGE_PROMPT

TIDY_TARGET_KINDS = {"user_fact", "self_fact", "important_event"}
DEFAULT_TIDY_CHUNK_ITEMS = 20
DEFAULT_TIDY_CHUNK_CHARS = 6000
DEFAULT_TIDY_MAX_TOKENS = 12000
_tidy_lock = threading.Lock()


def _tidy_chunk_items() -> int:
    try:
        value = int(os.environ.get("PUPU_MEMU_TIDY_CHUNK_SIZE", str(DEFAULT_TIDY_CHUNK_ITEMS)))
        return max(4, value)
    except Exception:
        return DEFAULT_TIDY_CHUNK_ITEMS


def _tidy_chunk_chars() -> int:
    try:
        value = int(os.environ.get("PUPU_MEMU_TIDY_CHUNK_CHARS", str(DEFAULT_TIDY_CHUNK_CHARS)))
        return max(1000, value)
    except Exception:
        return DEFAULT_TIDY_CHUNK_CHARS


def _tidy_max_tokens() -> int:
    try:
        value = int(os.environ.get("PUPU_MEMU_TIDY_MAX_TOKENS", str(DEFAULT_TIDY_MAX_TOKENS)))
        return max(600, value)
    except Exception:
        return DEFAULT_TIDY_MAX_TOKENS


def _strip_code_fence(raw_text: str) -> str:
    raw = (raw_text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines:
        lines = lines[1:]
    raw = "\n".join(lines)
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(raw_text)
    decoder = json.JSONDecoder()
    candidates: list[str] = []
    if cleaned:
        candidates.append(cleaned)
        brace_index = cleaned.find("{")
        if brace_index != -1:
            candidates.append(cleaned[brace_index:])

    seen: set[str] = set()
    for candidate in candidates:
        variants = [
            candidate,
            re.sub(r",\s*([}\]])", r"\1", candidate),
        ]
        for variant in variants:
            if variant in seen:
                continue
            seen.add(variant)
            try:
                parsed, _ = decoder.raw_decode(variant)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError("unable to parse maintenance response as JSON object")


def _normalize_id_list(values: object, allowed_ids: set[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        item_id = str(value or "").strip()
        if item_id and item_id in allowed_ids and item_id not in out:
            out.append(item_id)
    return out


def _normalize_reason_map(values: object, allowed_ids: set[str]) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in values.items():
        item_id = str(key or "").strip()
        if not item_id or item_id not in allowed_ids:
            continue
        reason = str(value or "").strip()
        if reason:
            out[item_id] = reason
    return out


def _reason_is_duplicate(reason: str) -> bool:
    normalized = reason.strip().lower()
    return "duplicate" in normalized or "重复" in reason


def _should_delete_legacy_source(record: dict[str, Any], reason: str) -> bool:
    if not record.get("legacy_key"):
        return False
    if _reason_is_duplicate(reason):
        return False
    return record.get("legacy_table") in {"user_facts", "self_facts", "important_events"}


def _build_tidy_record(
    item: dict[str, Any],
    *,
    legacy_events: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    payload, _parse_failed = _payload_from_item(item)
    kind = str(payload.get("kind") or item.get("memory_type") or "").strip()
    if kind not in TIDY_TARGET_KINDS:
        return None

    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return None

    text = _norm_text(payload.get("text") or item.get("summary") or item.get("content"))
    legacy_table = "user_facts" if kind == "user_fact" else "self_facts" if kind == "self_fact" else "important_events"
    legacy_key = str(payload.get("key") or payload.get("source_event_key") or "").strip()
    legacy_action = f"delete {legacy_table}:{legacy_key}" if legacy_key else "none"
    legacy_info: dict[str, Any] = {}
    if kind == "important_event" and legacy_key:
        legacy_row = legacy_events.get(legacy_key) or {}
        legacy_info = {
            "kind": str(legacy_row.get("kind") or "").strip(),
            "status": str(legacy_row.get("status") or "").strip(),
            "linked_task_id": legacy_row.get("linked_task_id"),
            "event_time": str(legacy_row.get("event_time") or "").strip(),
            "created_at": str(legacy_row.get("created_at") or "").strip(),
        }

    return {
        "item_id": item_id,
        "kind": kind,
        "text": text,
        "memory_type": str(item.get("memory_type") or "").strip(),
        "score": item.get("score"),
        "created_at": str(payload.get("created_at") or item.get("created_at") or "").strip(),
        "legacy_table": legacy_table,
        "legacy_key": legacy_key,
        "legacy_action": legacy_action,
        "source_event_key": str(payload.get("source_event_key") or "").strip(),
        "event_time": str(payload.get("event_time") or "").strip(),
        "legacy_info": legacy_info,
    }


def _iter_tidy_chunks(records: list[dict[str, Any]]):
    max_items = _tidy_chunk_items()
    max_chars = _tidy_chunk_chars()
    chunk: list[dict[str, Any]] = []
    chunk_chars = 0
    for record in records:
        record_chars = len(str(record.get("text") or ""))
        if chunk and (len(chunk) >= max_items or chunk_chars + record_chars > max_chars):
            yield chunk
            chunk = []
            chunk_chars = 0
        chunk.append(record)
        chunk_chars += record_chars
    if chunk:
        yield chunk


def _judge_tidy_chunk(
    *,
    identity_session: str,
    now: datetime,
    expire_days: int,
    chunk: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "identity_session": identity_session,
        "now": now.isoformat(timespec="seconds"),
        "expire_days": expire_days,
        "items": chunk,
    }
    raw_text = json_task(
        role="judge",
        model=JUDGE_MODEL,
        system=MEMU_TIDY_JUDGE_PROMPT,
        user_content=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=_tidy_max_tokens(),
        task_name="memu_tidy",
    )
    parsed = _parse_json_object(raw_text)
    allowed_ids = {str(item.get("item_id") or "") for item in chunk}
    drop_ids = _normalize_id_list(parsed.get("drop_ids", []), allowed_ids)
    reason_by_id = _normalize_reason_map(parsed.get("reason_by_id", {}), allowed_ids)
    notes = str(parsed.get("notes", "")).strip()
    for item_id in drop_ids:
        reason_by_id.setdefault(item_id, "judged_drop")
    return {
        "drop_ids": drop_ids,
        "reason_by_id": reason_by_id,
        "notes": notes,
    }


def _format_candidate_preview(candidates: list[dict[str, Any]], limit: int = 5) -> str:
    parts = []
    for item in candidates[:limit]:
        action = _legacy_source_action(item)
        preview = _preview(item.get("text"), 80)
        parts.append(
            f"{item.get('kind')}:{item.get('reason')}:{preview}"
            + (f" old={action}" if action != "none" else "")
        )
    return " | ".join(parts)


def _result_candidate_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    items = result.get("candidate_items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    candidates = result.get("candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    return []


def _busy_result(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "scanned": 0,
        "candidates": 0,
        "candidate_items": [],
        "deleted": 0,
        "failed": 0,
        "legacy_deleted": 0,
        "updated": 0,
        "reason_counts": {},
        "scanned_kind_counts": {},
        "drop_kind_counts": {},
        "judge_notes": [],
        "judge_failures": 0,
        "unknown_drop_ids": 0,
        "note": "memU tidy is already running",
        "status": "busy",
    }


def analyze_memu_tidy(
    identity_session: str,
    *,
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    _log(f"tidy analyze start identity={identity_session} expire_days={expire_days}")
    if not is_memu_long_term_enabled():
        _log(f"tidy analyze skipped status=disabled identity={identity_session}")
        return {
            "status": "disabled",
            "scanned": 0,
            "candidates": [],
            "scanned_kind_counts": {},
            "drop_kind_counts": {},
            "reason_counts": {},
            "judge_notes": [],
            "judge_failures": 0,
            "unknown_drop_ids": 0,
            "note": "memU disabled",
        }

    try:
        items = _list_items(identity_session, limit=10000)
    except Exception as exc:
        _log(f"tidy analyze skipped identity={identity_session} error={type(exc).__name__}: {exc}")
        return {
            "status": "unavailable",
            "scanned": 0,
            "candidates": [],
            "scanned_kind_counts": {},
            "drop_kind_counts": {},
            "reason_counts": {},
            "judge_notes": [],
            "judge_failures": 0,
            "unknown_drop_ids": 0,
            "note": f"memU skipped ({exc})",
        }

    run_at = now or datetime.now()
    legacy_events = _load_legacy_event_map(identity_session)
    records: list[dict[str, Any]] = []
    scanned_kind_counts: Counter[str] = Counter()
    for item in items:
        record = _build_tidy_record(item, legacy_events=legacy_events)
        if not record:
            continue
        records.append(record)
        scanned_kind_counts[record["kind"]] += 1

    if not records:
        _log(
            "tidy analyze done "
            f"identity={identity_session} scanned=0 candidates=0 kinds={{}} reasons={{}}"
        )
        return {
            "status": "ok",
            "scanned": 0,
            "candidates": [],
            "scanned_kind_counts": dict(scanned_kind_counts),
            "drop_kind_counts": {},
            "reason_counts": {},
            "judge_notes": [],
            "judge_failures": 0,
            "unknown_drop_ids": 0,
            "note": "",
        }

    candidates: list[dict[str, Any]] = []
    drop_kind_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    judge_notes: list[str] = []
    judge_failures = 0
    unknown_drop_ids = 0

    for batch_index, chunk in enumerate(_iter_tidy_chunks(records), start=1):
        chunk_chars = sum(len(str(item.get("text") or "")) for item in chunk)
        _log(
            "tidy judge start "
            f"identity={identity_session} batch={batch_index} items={len(chunk)} chars={chunk_chars}"
        )
        try:
            judged = _judge_tidy_chunk(
                identity_session=identity_session,
                now=run_at,
                expire_days=expire_days,
                chunk=chunk,
            )
        except Exception as exc:
            judge_failures += 1
            _log(
                "tidy judge failed "
                f"identity={identity_session} batch={batch_index} error={type(exc).__name__}: {_preview(exc, 500)}"
            )
            continue

        batch_drop_ids = set(judged.get("drop_ids") or [])
        batch_reason_by_id = dict(judged.get("reason_by_id") or {})
        batch_notes = str(judged.get("notes") or "").strip()
        if batch_notes:
            judge_notes.append(batch_notes)

        chunk_ids = {str(item.get("item_id") or "") for item in chunk}
        unknown_ids = sorted(item_id for item_id in batch_drop_ids if item_id not in chunk_ids)
        if unknown_ids:
            unknown_drop_ids += len(unknown_ids)
            _log(
                "tidy judge ignored unknown ids "
                f"identity={identity_session} batch={batch_index} ids={_json_compact(unknown_ids)}"
            )

        for record in chunk:
            item_id = record["item_id"]
            if item_id not in batch_drop_ids:
                continue
            reason = batch_reason_by_id.get(item_id, "judged_drop")
            candidate = dict(record)
            candidate["reason"] = reason
            candidate["delete_legacy"] = _should_delete_legacy_source(record, reason)
            candidate["legacy_action"] = _legacy_source_action(candidate)
            candidate["judge_note"] = batch_notes
            candidates.append(candidate)
            drop_kind_counts[candidate["kind"]] += 1
            reason_counts[reason] += 1
            _log(
                "maintenance candidate "
                f"identity={identity_session} item_id={candidate['item_id']} "
                f"kind={candidate['kind']} reason={candidate['reason']} "
                f"old_source_action={candidate['legacy_action']} "
                f"text_preview={_preview(candidate['text'])}"
            )

    analysis_status = "partial" if judge_failures else "ok"
    _log(
        "tidy analyze done "
        f"identity={identity_session} scanned={len(records)} candidates={len(candidates)} "
        f"kinds={_json_compact(dict(scanned_kind_counts))} reasons={_json_compact(dict(reason_counts))}"
    )
    return {
        "status": analysis_status,
        "scanned": len(records),
        "candidates": candidates,
        "scanned_kind_counts": dict(scanned_kind_counts),
        "drop_kind_counts": dict(drop_kind_counts),
        "reason_counts": dict(reason_counts),
        "judge_notes": judge_notes,
        "judge_failures": judge_failures,
        "unknown_drop_ids": unknown_drop_ids,
        "note": "",
    }


def _load_active_legacy_important_events(identity_session: str) -> list[dict[str, Any]]:
    from ..storage.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT source_event_key, title, kind, event_time, time_text,
                      details, followup_hint, confidence, status, linked_task_id
               FROM important_events
               WHERE session_id = ? AND status IN ('active', 'scheduled')
               ORDER BY last_seen_at DESC, created_at DESC, id DESC""",
            (identity_session,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _sync_status_note(status: dict[str, Any]) -> str:
    value = str(status.get("status") or "unknown")
    checked = int(status.get("checked") or 0)
    missing = int(status.get("missing") or 0)
    synced = int(status.get("synced") or 0)
    return f"{value}:checked={checked},missing={missing},synced={synced}"


def _check_legacy_source_sync(identity_session: str) -> dict[str, Any]:
    events = _load_active_legacy_important_events(identity_session)
    from .memu_adapter import sync_missing_memu_important_events

    return sync_missing_memu_important_events(identity_session, events, dry_run=True)


def _sync_legacy_sources_after_tidy(identity_session: str) -> dict[str, Any]:
    events = _load_active_legacy_important_events(identity_session)
    from .memu_adapter import sync_missing_memu_important_events

    return sync_missing_memu_important_events(identity_session, events)


def _run_memu_tidy_unlocked(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    _log(f"tidy start identity={identity_session}")
    mode = str(mode or "apply").strip().lower()
    if mode not in {"apply", "check"}:
        raise ValueError("memU tidy mode must be 'apply' or 'check'")

    analysis = analyze_memu_tidy(identity_session, now=now, expire_days=expire_days)
    candidates = list(analysis.get("candidates") or [])
    if analysis.get("status") == "disabled":
        return {
            "mode": mode,
            "scanned": int(analysis.get("scanned") or 0),
            "candidates": len(candidates),
            "candidate_items": candidates,
            "deleted": 0,
            "failed": 0,
            "legacy_deleted": 0,
            "updated": 0,
            "reason_counts": analysis.get("reason_counts") or {},
            "scanned_kind_counts": analysis.get("scanned_kind_counts") or {},
            "drop_kind_counts": analysis.get("drop_kind_counts") or {},
            "judge_notes": analysis.get("judge_notes") or [],
            "judge_failures": int(analysis.get("judge_failures") or 0),
            "unknown_drop_ids": int(analysis.get("unknown_drop_ids") or 0),
            "note": analysis.get("note") or "",
            "status": analysis.get("status") or "disabled",
        }

    if mode == "check":
        sync_status = _check_legacy_source_sync(identity_session)
        note = (
            f"memU mode=check, scanned={analysis['scanned']}, candidates={len(candidates)}, "
            f"reasons={analysis['reason_counts']}"
        )
        note += f", source_sync={_sync_status_note(sync_status)}"
        if analysis.get("judge_failures"):
            note += f", judge_failures={analysis['judge_failures']}"
        if analysis.get("unknown_drop_ids"):
            note += f", unknown_drop_ids={analysis['unknown_drop_ids']}"
        preview = _format_candidate_preview(candidates)
        if preview:
            note += f", preview={preview}"
        if analysis.get("judge_notes"):
            note += f", notes={' | '.join(str(item) for item in analysis['judge_notes'][:3])}"
        _log(f"tidy check done identity={identity_session} note={note}")
        return {
            "mode": mode,
            "scanned": int(analysis.get("scanned") or 0),
            "candidates": len(candidates),
            "candidate_items": candidates,
            "deleted": 0,
            "failed": 0,
            "legacy_deleted": 0,
            "updated": 0,
            "reason_counts": analysis.get("reason_counts") or {},
            "scanned_kind_counts": analysis.get("scanned_kind_counts") or {},
            "drop_kind_counts": analysis.get("drop_kind_counts") or {},
            "judge_notes": analysis.get("judge_notes") or [],
            "judge_failures": int(analysis.get("judge_failures") or 0),
            "unknown_drop_ids": int(analysis.get("unknown_drop_ids") or 0),
            "source_sync": sync_status,
            "note": note,
            "status": analysis.get("status") or "ok",
        }

    service = None
    try:
        service = _get_service()
    except Exception as exc:
        note = f"memU unavailable ({exc})"
        _log(f"tidy apply skipped identity={identity_session} error={type(exc).__name__}: {_preview(exc, 500)}")
        return {
            "mode": mode,
            "scanned": int(analysis.get("scanned") or 0),
            "candidates": len(candidates),
            "candidate_items": candidates,
            "deleted": 0,
            "failed": len(candidates),
            "legacy_deleted": 0,
            "updated": 0,
            "reason_counts": analysis.get("reason_counts") or {},
            "scanned_kind_counts": analysis.get("scanned_kind_counts") or {},
            "drop_kind_counts": analysis.get("drop_kind_counts") or {},
            "judge_notes": analysis.get("judge_notes") or [],
            "judge_failures": int(analysis.get("judge_failures") or 0),
            "unknown_drop_ids": int(analysis.get("unknown_drop_ids") or 0),
            "note": note,
            "status": "unavailable",
        }

    async def _delete(items_to_delete: list[dict[str, Any]]) -> tuple[int, int, int]:
        deleted = 0
        failed = 0
        legacy_deleted = 0
        for candidate in items_to_delete:
            item_id = str(candidate.get("item_id") or "")
            if not item_id:
                continue
            _log(
                "maintenance delete item "
                f"identity={identity_session} item_id={item_id} kind={candidate.get('kind')} "
                f"reason={candidate.get('reason')} old_source_action={candidate.get('legacy_action')}"
            )
            try:
                await service.delete_memory_item(memory_id=item_id, user={"identity_session": identity_session})
            except Exception as exc:
                failed += 1
                _log(
                    "maintenance delete failed "
                    f"identity={identity_session} item_id={item_id} error={type(exc).__name__}: {_preview(exc, 500)}"
                )
                continue
            deleted += 1
            if candidate.get("delete_legacy"):
                try:
                    legacy_deleted += _delete_legacy_source(identity_session, candidate)
                except Exception as exc:
                    _log(
                        "maintenance legacy delete failed "
                        f"identity={identity_session} item_id={item_id} "
                        f"old_source_action={candidate.get('legacy_action')} "
                        f"error={type(exc).__name__}: {_preview(exc, 500)}"
                    )
        return deleted, failed, legacy_deleted

    deleted = 0
    failed = 0
    legacy_deleted = 0
    if candidates:
        with _op_lock:
            deleted, failed, legacy_deleted = _run(_delete(candidates))
    sync_status = _sync_legacy_sources_after_tidy(identity_session)

    note = (
        f"memU mode=apply, scanned={analysis['scanned']}, candidates={len(candidates)}, "
        f"reasons={analysis['reason_counts']}, deleted={deleted}, "
        f"legacy_deleted={legacy_deleted}, failed={failed}, "
        f"source_sync={_sync_status_note(sync_status)}"
    )
    if analysis.get("judge_failures"):
        note += f", judge_failures={analysis['judge_failures']}"
    if analysis.get("unknown_drop_ids"):
        note += f", unknown_drop_ids={analysis['unknown_drop_ids']}"
    preview = _format_candidate_preview(candidates)
    if preview:
        note += f", preview={preview}"
    if analysis.get("judge_notes"):
        note += f", notes={' | '.join(str(item) for item in analysis['judge_notes'][:3])}"
    _log(f"maintenance done identity={identity_session} deleted={deleted} updated=0 note={note}")
    return {
        "mode": mode,
        "scanned": int(analysis.get("scanned") or 0),
        "candidates": len(candidates),
        "candidate_items": candidates,
        "deleted": deleted,
        "failed": failed,
        "legacy_deleted": legacy_deleted,
        "updated": 0,
        "reason_counts": analysis.get("reason_counts") or {},
        "scanned_kind_counts": analysis.get("scanned_kind_counts") or {},
        "drop_kind_counts": analysis.get("drop_kind_counts") or {},
        "judge_notes": analysis.get("judge_notes") or [],
        "judge_failures": int(analysis.get("judge_failures") or 0),
        "unknown_drop_ids": int(analysis.get("unknown_drop_ids") or 0),
        "source_sync": sync_status,
        "note": note,
        "status": analysis.get("status") or "ok",
    }


def run_memu_tidy(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
) -> dict[str, Any]:
    normalized_mode = str(mode or "apply").strip().lower()
    if normalized_mode not in {"apply", "check"}:
        raise ValueError("memU tidy mode must be 'apply' or 'check'")

    if not _tidy_lock.acquire(blocking=False):
        _log(f"tidy skipped status=busy identity={identity_session} mode={normalized_mode}")
        return _busy_result(normalized_mode)

    try:
        return _run_memu_tidy_unlocked(
            identity_session,
            mode=normalized_mode,
            now=now,
            expire_days=expire_days,
        )
    finally:
        _tidy_lock.release()


def format_memu_tidy_report(
    result: dict[str, Any],
    *,
    identity_session: str,
    mode: str,
    trigger: str | None = None,
) -> str:
    title = "memU tidy 检查完成" if mode == "check" else "memU tidy 完成"
    if trigger:
        title = f"{title}（{trigger}）"

    lines = [title]
    lines.append(f"- identity: {identity_session}")
    lines.append(f"- 扫描条数：{int(result.get('scanned') or 0)}")
    lines.append(f"- 候选条数：{int(result.get('candidates') or 0)}")
    if mode == "apply":
        lines.append(f"- 删除条数：{int(result.get('deleted') or 0)}")
        lines.append(f"- 旧库删除条数：{int(result.get('legacy_deleted') or 0)}")
        lines.append(f"- 失败条数：{int(result.get('failed') or 0)}")
    if result.get("scanned_kind_counts"):
        lines.append(f"- 扫描 kinds：{_json_compact(result['scanned_kind_counts'])}")
    if result.get("reason_counts"):
        lines.append(f"- 删除 reasons：{_json_compact(result['reason_counts'])}")
    if result.get("judge_failures"):
        lines.append(f"- judge 失败批次：{int(result.get('judge_failures') or 0)}")
    if result.get("unknown_drop_ids"):
        lines.append(f"- judge 无效 id：{int(result.get('unknown_drop_ids') or 0)}")
    preview = _format_candidate_preview(_result_candidate_items(result))
    if preview:
        lines.append(f"- 预览：{preview}")
    judge_notes = [str(item).strip() for item in (result.get("judge_notes") or []) if str(item).strip()]
    if judge_notes:
        lines.append(f"- notes：{' | '.join(judge_notes[:3])}")
    if result.get("note"):
        lines.append(f"- 结果：{result['note']}")
    return "\n".join(lines)


def run_memu_maintenance(
    identity_session: str,
    *,
    mode: str = "apply",
    now: datetime | None = None,
    expire_days: int = 14,
    trigger: str = "manual",
) -> str:
    from .memu_adapter import _log as _legacy_log

    result = run_memu_tidy(identity_session, mode=mode, now=now, expire_days=expire_days)
    _legacy_log(
        f"maintenance wrapper identity={identity_session} mode={mode} status={result.get('status')}"
    )
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


def format_memu_tidy_report(
    result: dict[str, Any],
    *,
    identity_session: str,
    mode: str,
    trigger: str | None = None,
) -> str:
    title = "memU tidy check complete" if mode == "check" else "memU tidy complete"
    if trigger:
        title = f"{title} ({trigger})"

    lines = [title]
    lines.append(f"- identity: {identity_session}")
    lines.append(f"- scanned: {int(result.get('scanned') or 0)}")
    lines.append(f"- candidates: {int(result.get('candidates') or 0)}")
    if mode == "apply":
        lines.append(f"- deleted: {int(result.get('deleted') or 0)}")
        lines.append(f"- legacy_deleted: {int(result.get('legacy_deleted') or 0)}")
        lines.append(f"- failed: {int(result.get('failed') or 0)}")
    if result.get("scanned_kind_counts"):
        lines.append(f"- scanned_kinds: {_json_compact(result['scanned_kind_counts'])}")
    if result.get("reason_counts"):
        lines.append(f"- reason_counts: {_json_compact(result['reason_counts'])}")
    if result.get("judge_failures"):
        lines.append(f"- judge_failures: {int(result.get('judge_failures') or 0)}")
    if result.get("unknown_drop_ids"):
        lines.append(f"- unknown_drop_ids: {int(result.get('unknown_drop_ids') or 0)}")
    if result.get("source_sync"):
        lines.append(f"- source_sync: {_sync_status_note(result['source_sync'])}")
    preview = _format_candidate_preview(_result_candidate_items(result))
    if preview:
        lines.append(f"- preview: {preview}")
    judge_notes = [str(item).strip() for item in (result.get("judge_notes") or []) if str(item).strip()]
    if judge_notes:
        lines.append(f"- notes: {' | '.join(judge_notes[:3])}")
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
