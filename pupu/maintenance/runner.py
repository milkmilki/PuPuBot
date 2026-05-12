"""Top-level maintenance workflow orchestration."""

import threading
import traceback
from datetime import datetime

from ..message_sources import PROACTIVE, SCHEDULED
from ..memory_index import is_memu_long_term_enabled, run_memu_maintenance
from ..storage.db import get_conn, init_db
from .constants import BUSY_REPORT_PREFIX
from .dedupe import (
    _dedupe_events,
    _dedupe_important_events,
    _dedupe_scheduled_tasks,
    _dedupe_summaries,
)
from .history import _list_all_session_ids, _record_maintenance_run
from .model_compaction import _run_model_compaction
from .prune import (
    _prune_old_chat_messages,
    _prune_old_disabled_scheduled_tasks,
    _prune_old_internal_messages,
)
from .snapshot import _build_session_snapshot

_maintenance_lock = threading.Lock()


def run_memory_maintenance(
    trigger: str = "manual",
    include_model: bool = True,
    now: datetime | None = None,
) -> str:
    if not _maintenance_lock.acquire(blocking=False):
        return f"{BUSY_REPORT_PREFIX}，等它收尾一个。"

    run_at = now or datetime.now()
    run_date = run_at.date().isoformat()
    try:
        init_db()
        conn = get_conn()
        try:
            session_ids = _list_all_session_ids(conn)

            report = {
                "sessions": len(session_ids),
                "deduped_summaries": _dedupe_summaries(conn),
                "deduped_events": _dedupe_events(conn),
                "deduped_important_events": _dedupe_important_events(conn),
                "deduped_tasks": _dedupe_scheduled_tasks(conn),
                "deleted_chat_messages": 0,
                "deleted_internal_messages": 0,
                "deleted_disabled_tasks": _prune_old_disabled_scheduled_tasks(
                    conn,
                    run_at,
                ),
                "model_dropped_summaries": 0,
                "model_merged_summaries": 0,
                "model_dropped_important_events": 0,
                "model_updated_important_events": 0,
                "model_deleted_facts": 0,
                "model_updated_facts": 0,
                "memu_deleted": 0,
                "memu_updated": 0,
                "model_notes": [],
            }

            for session_id in session_ids:
                report["deleted_chat_messages"] += _prune_old_chat_messages(conn, session_id)
                for source in (SCHEDULED, PROACTIVE):
                    report["deleted_internal_messages"] += _prune_old_internal_messages(
                        conn,
                        session_id,
                        source,
                    )

            conn.commit()

            if include_model and is_memu_long_term_enabled():
                for session_id in session_ids:
                    print(f"[pupu][maintenance] session={session_id} phase=memu start")
                    session_result = run_memu_maintenance(session_id)
                    report["memu_deleted"] += int(session_result.get("deleted") or 0)
                    report["memu_updated"] += int(session_result.get("updated") or 0)
                    if session_result.get("note"):
                        report["model_notes"].append(
                            f"{session_id}: {session_result['note']}"
                        )
                    print(
                        "[pupu][maintenance] "
                        f"session={session_id} phase=memu done "
                        f"deleted={session_result.get('deleted', 0)} "
                        f"updated={session_result.get('updated', 0)}"
                    )
            elif include_model:
                for session_id in session_ids:
                    snapshot = _build_session_snapshot(conn, session_id)
                    try:
                        session_result = _run_model_compaction(conn, snapshot)
                    except Exception as exc:
                        report["model_notes"].append(
                            f"{session_id}: model cleanup skipped ({exc})"
                        )
                        continue
                    report["model_dropped_summaries"] += session_result["dropped_summaries"]
                    report["model_merged_summaries"] += session_result["merged_summaries"]
                    report["model_dropped_important_events"] += session_result[
                        "dropped_important_events"
                    ]
                    report["model_updated_important_events"] += session_result[
                        "updated_important_events"
                    ]
                    report["model_deleted_facts"] += session_result["deleted_facts"]
                    report["model_updated_facts"] += session_result["updated_facts"]
                    if session_result["note"]:
                        report["model_notes"].append(
                            f"{session_id}: {session_result['note']}"
                        )
                    conn.commit()

            summary_lines = [
                f"记忆整理完成（{trigger}）",
                f"- 会话数：{report['sessions']}",
                f"- 去重摘要：{report['deduped_summaries']}",
                f"- 去重旧好感度记录：{report['deduped_events']}",
                f"- 去重重要事件：{report['deduped_important_events']}",
                f"- 去重定时任务：{report['deduped_tasks']}",
                f"- 清理旧聊天消息：{report['deleted_chat_messages']}",
                f"- 清理旧内部消息：{report['deleted_internal_messages']}",
                f"- 清理旧取消定时任务：{report['deleted_disabled_tasks']}",
            ]
            if include_model:
                summary_lines.extend(
                    [
                        f"- 模型删除摘要：{report['model_dropped_summaries']}",
                        f"- 模型合并摘要：{report['model_merged_summaries']}",
                        f"- 模型删除重要事件：{report['model_dropped_important_events']}",
                        f"- 模型重排重要事件：{report['model_updated_important_events']}",
                        f"- 模型删除事实：{report['model_deleted_facts']}",
                        f"- 模型更新事实：{report['model_updated_facts']}",
                        f"- memU 删除记忆：{report['memu_deleted']}",
                        f"- memU 更新记忆：{report['memu_updated']}",
                    ]
                )
                if report["model_notes"]:
                    summary_lines.append("- 模型备注：")
                    summary_lines.extend(f"  {note}" for note in report["model_notes"][:6])

            report_text = "\n".join(summary_lines)
            _record_maintenance_run(conn, run_date, trigger, "success", report_text)
            conn.commit()
            return report_text
        except Exception:
            failure = "记忆整理失败\n" + traceback.format_exc()
            try:
                _record_maintenance_run(conn, run_date, trigger, "failed", failure[:4000])
                conn.commit()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    finally:
        _maintenance_lock.release()
