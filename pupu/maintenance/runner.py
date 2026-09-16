"""Top-level maintenance workflow orchestration."""

import threading
import traceback
from datetime import datetime
from pathlib import Path

from ..memory_index import run_semantic_tidy
from ..message_sources import PROACTIVE, SCHEDULED
from ..sessions import OWNER_SESSION
from ..storage.db import get_conn, get_db_path, init_db
from .constants import BUSY_REPORT_PREFIX
from .dedupe import (
    _dedupe_events,
    _dedupe_scheduled_tasks,
    _dedupe_summaries,
)
from .history import _list_all_session_ids, _record_maintenance_run
from .metrics import measure_memory_density
from .model_compaction import _run_model_compaction
from .prune import (
    _prune_old_chat_messages,
    _prune_old_disabled_scheduled_tasks,
    _prune_old_internal_messages,
)
from .snapshot import _build_session_snapshot

_maintenance_locks: dict[str, threading.Lock] = {}
_maintenance_locks_guard = threading.Lock()


def _maintenance_lock_for_current_instance() -> threading.Lock:
    db_key = str(Path(get_db_path()).resolve())
    with _maintenance_locks_guard:
        return _maintenance_locks.setdefault(db_key, threading.Lock())


def run_memory_maintenance(
    trigger: str = "manual",
    include_model: bool = True,
    now: datetime | None = None,
    semantic_mode: str = "apply",
) -> str:
    maintenance_lock = _maintenance_lock_for_current_instance()
    if not maintenance_lock.acquire(blocking=False):
        return f"{BUSY_REPORT_PREFIX}，等它收尾一个。"

    run_at = now or datetime.now()
    run_date = run_at.date().isoformat()
    tidy_mode = str(semantic_mode or "apply").strip().lower()
    if tidy_mode not in {"apply", "check"}:
        raise ValueError("semantic_mode must be 'apply' or 'check'")
    try:
        init_db()
        conn = get_conn()
        try:
            session_ids = _list_all_session_ids(conn)
            session_ids.sort(key=lambda session_id: session_id == OWNER_SESSION)
            before_metrics = measure_memory_density(conn)

            if tidy_mode == "check":
                report = {
                    "sessions": len(session_ids),
                    "model_dropped_summaries": 0,
                    "model_merged_summaries": 0,
                    "model_updated_event_threads": 0,
                    "model_merged_event_threads": 0,
                    "model_event_rollups": 0,
                    "model_deleted_facts": 0,
                    "model_updated_facts": 0,
                    "model_fact_conflicts": 0,
                    "semantic_status": "not_run",
                    "model_notes": [],
                }

                if include_model:
                    processed_fact_ids: set[int] = set()
                    for session_id in session_ids:
                        print(
                            "[pupu][maintenance] "
                            f"session={session_id} phase=model_check start"
                        )
                        snapshot = _build_session_snapshot(conn, session_id)
                        snapshot["facts"] = [
                            fact
                            for fact in snapshot.get("facts", [])
                            if int(fact["id"]) not in processed_fact_ids
                        ]
                        try:
                            session_result = _run_model_compaction(
                                conn,
                                snapshot,
                                apply=False,
                                run_date=run_date,
                                trigger=trigger,
                            )
                        except Exception as exc:
                            conn.rollback()
                            report["model_notes"].append(
                                f"{session_id}: model cleanup skipped ({exc})"
                            )
                            continue
                        processed_fact_ids.update(
                            int(fact["id"]) for fact in snapshot["facts"]
                        )
                        report["model_dropped_summaries"] += int(
                            session_result.get("dropped_summaries") or 0
                        )
                        report["model_merged_summaries"] += int(
                            session_result.get("merged_summaries") or 0
                        )
                        report["model_updated_event_threads"] += int(
                            session_result.get("updated_event_threads") or 0
                        )
                        report["model_merged_event_threads"] += int(
                            session_result.get("merged_event_threads") or 0
                        )
                        report["model_event_rollups"] += int(
                            session_result.get("event_rollups") or 0
                        )
                        report["model_deleted_facts"] += int(
                            session_result.get("deleted_facts") or 0
                        )
                        report["model_updated_facts"] += int(
                            session_result.get("updated_facts") or 0
                        )
                        report["model_fact_conflicts"] += int(
                            session_result.get("fact_conflicts") or 0
                        )
                        if session_result.get("note"):
                            report["model_notes"].append(
                                f"{session_id}: {session_result['note']}"
                            )
                        print(
                            "[pupu][maintenance] "
                            f"session={session_id} phase=model_check done "
                            "dropped_summaries="
                            f"{session_result.get('dropped_summaries', 0)} "
                            f"deleted_facts={session_result.get('deleted_facts', 0)}"
                        )
                semantic_result = run_semantic_tidy(
                    OWNER_SESSION,
                    mode="check",
                    now=run_at,
                )
                report["semantic_status"] = str(
                    semantic_result.get("status") or "unknown"
                )

                summary_lines = [
                    f"记忆整理检查完成（{trigger}）",
                    f"- 会话数：{report['sessions']}",
                    f"- 模型删除摘要：{report['model_dropped_summaries']}",
                    f"- 模型合并摘要：{report['model_merged_summaries']}",
                    f"- 模型更新事件线：{report['model_updated_event_threads']}",
                    f"- 模型合并事件线：{report['model_merged_event_threads']}",
                    f"- 模型生成事件 checkpoint：{report['model_event_rollups']}",
                    f"- 模型归档事实：{report['model_deleted_facts']}",
                    f"- 模型更新事实：{report['model_updated_facts']}",
                    f"- 事实冲突：{report['model_fact_conflicts']}",
                    f"- 语义索引状态：{report['semantic_status']}",
                    f"- 活跃 facts：{before_metrics['active_facts']}",
                    f"- 活跃事件线：{before_metrics['active_event_threads']}",
                    (
                        "- Fact 证据覆盖率："
                        f"{before_metrics['fact_evidence_coverage']:.1%}"
                    ),
                    f"- 当前投影字符数：{before_metrics['projection_chars']}",
                    f"- 证据/投影压缩比：{before_metrics['compression_ratio']:.2f}",
                    f"- 已归档原始消息：{before_metrics['archived_messages']}",
                ]
                if report["model_notes"]:
                    summary_lines.append("- 模型备注：")
                    summary_lines.extend(
                        f"  {note}" for note in report["model_notes"][:6]
                    )

                report_text = "\n".join(summary_lines)
                return report_text

            report = {
                "sessions": len(session_ids),
                "deduped_summaries": _dedupe_summaries(conn),
                "deduped_events": _dedupe_events(conn),
                "deduped_tasks": _dedupe_scheduled_tasks(conn),
                "deleted_chat_messages": 0,
                "deleted_internal_messages": 0,
                "deleted_disabled_tasks": _prune_old_disabled_scheduled_tasks(
                    conn,
                    run_at,
                ),
                "model_dropped_summaries": 0,
                "model_merged_summaries": 0,
                "model_updated_event_threads": 0,
                "model_merged_event_threads": 0,
                "model_event_rollups": 0,
                "model_deleted_facts": 0,
                "model_updated_facts": 0,
                "model_fact_conflicts": 0,
                "semantic_status": "not_run",
                "model_notes": [],
            }

            for session_id in session_ids:
                report["deleted_chat_messages"] += _prune_old_chat_messages(
                    conn, session_id
                )
                for source in (SCHEDULED, PROACTIVE):
                    report["deleted_internal_messages"] += _prune_old_internal_messages(
                        conn,
                        session_id,
                        source,
                    )

            conn.commit()

            if include_model:
                processed_fact_ids: set[int] = set()
                for session_id in session_ids:
                    print(f"[pupu][maintenance] session={session_id} phase=model start")
                    snapshot = _build_session_snapshot(conn, session_id)
                    snapshot["facts"] = [
                        fact
                        for fact in snapshot.get("facts", [])
                        if int(fact["id"]) not in processed_fact_ids
                    ]
                    try:
                        session_result = _run_model_compaction(
                            conn,
                            snapshot,
                            apply=True,
                            run_date=run_date,
                            trigger=trigger,
                        )
                    except Exception as exc:
                        conn.rollback()
                        report["model_notes"].append(
                            f"{session_id}: model cleanup skipped ({exc})"
                        )
                        continue
                    processed_fact_ids.update(
                        int(fact["id"]) for fact in snapshot["facts"]
                    )
                    report["model_dropped_summaries"] += int(
                        session_result.get("dropped_summaries") or 0
                    )
                    report["model_merged_summaries"] += int(
                        session_result.get("merged_summaries") or 0
                    )
                    report["model_updated_event_threads"] += int(
                        session_result.get("updated_event_threads") or 0
                    )
                    report["model_merged_event_threads"] += int(
                        session_result.get("merged_event_threads") or 0
                    )
                    report["model_event_rollups"] += int(
                        session_result.get("event_rollups") or 0
                    )
                    report["model_deleted_facts"] += int(
                        session_result.get("deleted_facts") or 0
                    )
                    report["model_updated_facts"] += int(
                        session_result.get("updated_facts") or 0
                    )
                    report["model_fact_conflicts"] += int(
                        session_result.get("fact_conflicts") or 0
                    )
                    if session_result.get("note"):
                        report["model_notes"].append(
                            f"{session_id}: {session_result['note']}"
                        )
                    print(
                        "[pupu][maintenance] "
                        f"session={session_id} phase=model done "
                        "dropped_summaries="
                        f"{session_result.get('dropped_summaries', 0)} "
                        f"deleted_facts={session_result.get('deleted_facts', 0)}"
                    )
                conn.commit()

            semantic_result = run_semantic_tidy(
                OWNER_SESSION,
                mode=tidy_mode,
                now=run_at,
            )
            report["semantic_status"] = str(semantic_result.get("status") or "unknown")
            after_metrics = measure_memory_density(conn)

            summary_lines = [
                f"记忆整理完成（{trigger}）",
                f"- 会话数：{report['sessions']}",
                f"- 去重摘要：{report['deduped_summaries']}",
                f"- 去重旧好感度记录：{report['deduped_events']}",
                f"- 去重定时任务：{report['deduped_tasks']}",
                f"- 归档旧聊天消息：{report['deleted_chat_messages']}",
                f"- 归档旧内部消息：{report['deleted_internal_messages']}",
                f"- 清理旧取消定时任务：{report['deleted_disabled_tasks']}",
                f"- 语义索引状态：{report['semantic_status']}",
            ]
            if include_model:
                summary_lines.extend(
                    [
                        f"- 模型删除摘要：{report['model_dropped_summaries']}",
                        f"- 模型合并摘要：{report['model_merged_summaries']}",
                        f"- 模型更新事件线：{report['model_updated_event_threads']}",
                        f"- 模型合并事件线：{report['model_merged_event_threads']}",
                        f"- 模型生成事件 checkpoint：{report['model_event_rollups']}",
                        f"- 模型归档事实：{report['model_deleted_facts']}",
                        f"- 模型更新事实：{report['model_updated_facts']}",
                        f"- 事实冲突：{report['model_fact_conflicts']}",
                        (
                            "- 活跃 facts："
                            f"{before_metrics['active_facts']} → "
                            f"{after_metrics['active_facts']}"
                        ),
                        (
                            "- 活跃事件线："
                            f"{before_metrics['active_event_threads']} → "
                            f"{after_metrics['active_event_threads']}"
                        ),
                        (
                            "- Fact 证据覆盖率："
                            f"{before_metrics['fact_evidence_coverage']:.1%} → "
                            f"{after_metrics['fact_evidence_coverage']:.1%}"
                        ),
                        (
                            "- 记忆投影字符数："
                            f"{before_metrics['projection_chars']} → "
                            f"{after_metrics['projection_chars']}"
                        ),
                        (
                            "- 证据/投影压缩比："
                            f"{before_metrics['compression_ratio']:.2f} → "
                            f"{after_metrics['compression_ratio']:.2f}"
                        ),
                        f"- 已归档原始消息：{after_metrics['archived_messages']}",
                    ]
                )
                if report["model_notes"]:
                    summary_lines.append("- 模型备注：")
                    summary_lines.extend(
                        f"  {note}" for note in report["model_notes"][:6]
                    )

            report_text = "\n".join(summary_lines)
            _record_maintenance_run(conn, run_date, trigger, "success", report_text)
            conn.commit()
            return report_text
        except Exception:
            failure = "记忆整理失败\n" + traceback.format_exc()
            try:
                _record_maintenance_run(
                    conn, run_date, trigger, "failed", failure[:4000]
                )
                conn.commit()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    finally:
        maintenance_lock.release()
