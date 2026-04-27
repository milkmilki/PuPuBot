"""Scheduler/reminder tool server."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..base import BuiltinToolServer, ToolContext, ToolSpec


def _normalize_run_at_iso(value: str) -> tuple[str | None, str | None]:
    value = (value or "").strip()
    if not value:
        return None, "run_at 不能为空"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.isoformat(timespec="seconds"), None
    except Exception:
        return None, (
            f"无法解析 run_at：{value!r}，请用 ISO 格式，例如 2026-04-26T15:30:00"
        )


def _parse_local_run_at(run_at_str: str):
    try:
        dt = datetime.fromisoformat(str(run_at_str).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def manage_scheduled_task(session_id: str, tool_input: dict) -> str:
    from ...memory import (
        MAX_SCHEDULED_TASKS_PER_SESSION,
        cancel_matching_scheduled_tasks,
        cancel_scheduled_task,
        count_scheduled_tasks,
        create_scheduled_task,
        get_summary_trigger_progress,
        list_scheduled_tasks,
        reschedule_matching_scheduled_tasks,
    )

    action = (tool_input.get("action") or "").strip().lower()
    if action == "list":
        try:
            from ...agent import REVIEW_INTERVAL as review_interval
        except Exception:
            review_interval = 8
        progress = get_summary_trigger_progress(session_id, review_interval)
        summary_line = (
            "总结进度："
            f"{progress['pending']}/{progress['interval']}，"
            f"还差 {progress['remaining']} 轮触发自动总结"
        )

        rows = list_scheduled_tasks(session_id)
        if not rows:
            return f"{summary_line}\n当前没有待执行的定时任务"

        lines = []
        for index, row in enumerate(rows, start=1):
            title = row.get("title") or "提醒"
            run_at = _parse_local_run_at(str(row["run_at"]))
            overdue = ""
            if run_at is not None and run_at < datetime.now():
                overdue = " ⚠️ 这个时间早于当前时间，会被立刻触发；如果年份填错了请先取消再重建"
            interval = (
                f" interval={row['interval_seconds']}"
                if row.get("interval_seconds")
                else ""
            )
            lines.append(
                f"- #{index} id={row['id']} | {row['run_at']} | repeat={row['repeat_kind']}{interval} | 《{title}》{overdue}\n"
                f"  说明：{(row.get('instruction') or '')[:120]}"
            )
        return summary_line + "\n定时任务列表：\n" + "\n".join(lines)

    if action == "cancel_matching":
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return "cancel_matching 需要 query，用来描述要取消的提醒"
        cancelled = cancel_matching_scheduled_tasks(session_id, query)
        if not cancelled:
            return f"没有找到匹配 {query!r} 的待执行定时任务"
        ids = ", ".join(f"id={row['id']}" for row in cancelled[:8])
        more = f" 等 {len(cancelled)} 个" if len(cancelled) > 8 else ""
        return f"已取消匹配 {query!r} 的定时任务：{ids}{more}"

    if action == "reschedule_matching":
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return "reschedule_matching 需要 query，用来描述要改时间的提醒"
        run_raw = tool_input.get("run_at")
        if not run_raw:
            return "reschedule_matching 需要 run_at（新的本地时间 ISO 字符串）"
        run_norm, error = _normalize_run_at_iso(str(run_raw))
        if error:
            return error

        repeat = str(tool_input.get("repeat") or "").strip().lower()
        repeat_alias = {
            "每天": "daily",
            "每日": "daily",
            "每周": "weekly",
            "每月": "monthly",
            "每年": "yearly",
            "everyday": "daily",
        }
        repeat = repeat_alias.get(repeat, repeat)
        interval_seconds = None
        if repeat:
            if repeat not in ("once", "daily", "weekly", "monthly", "yearly", "interval"):
                return f"不支持的 repeat：{repeat}，请用 once / daily / weekly / monthly / yearly / interval"
            if repeat == "interval":
                try:
                    interval_seconds = int(tool_input.get("interval_seconds"))
                except (TypeError, ValueError):
                    return "repeat 为 interval 时必须提供正整数 interval_seconds（秒）"
                if interval_seconds < 60:
                    return "interval_seconds 最少 60 秒"
                if interval_seconds > 86400 * 7:
                    return "interval_seconds 最多 7 天（604800 秒）"

        updated = reschedule_matching_scheduled_tasks(
            session_id,
            query,
            run_norm,
            repeat or None,
            interval_seconds,
        )
        if not updated:
            return f"没有找到匹配 {query!r} 的待执行定时任务"
        ids = ", ".join(f"id={row['id']} {row.get('old_run_at')}->{row['run_at']}" for row in updated[:8])
        more = f" 等 {len(updated)} 个" if len(updated) > 8 else ""
        return f"已更新匹配 {query!r} 的定时任务：{ids}{more}"

    if action == "cancel":
        task_id = tool_input.get("task_id")
        display_index = None
        if task_id is None:
            task_index = tool_input.get("task_index")
            if task_index is None:
                return "cancel 需要 task_id 或 task_index，可以先用 action=list 查看"
            try:
                display_index = int(task_index)
            except (TypeError, ValueError):
                return "task_index 必须是整数"
            rows = list_scheduled_tasks(session_id)
            if display_index < 1 or display_index > len(rows):
                return f"没有找到 #{display_index} 的任务，可以先用 action=list 查看当前序号"
            task_id = rows[display_index - 1]["id"]
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return "task_id 必须是整数"
        if cancel_scheduled_task(session_id, task_id):
            prefix = f"#{display_index} " if display_index is not None else ""
            return f"已取消定时任务 {prefix}id={task_id}"
        return f"没有找到 id={task_id} 的任务，或者它不属于当前会话、已被取消"

    if action == "create":
        instruction = (tool_input.get("instruction") or "").strip()
        if not instruction:
            return "create 需要 instruction：到点时你要对用户说什么或提醒什么"
        if len(instruction) > 2000:
            return "instruction 太长（上限 2000 字），请缩短"

        run_raw = tool_input.get("run_at")
        if not run_raw:
            return "create 需要 run_at（本地时间 ISO 字符串）"
        run_norm, error = _normalize_run_at_iso(str(run_raw))
        if error:
            return error

        run_dt = _parse_local_run_at(run_norm)
        if run_dt is not None and run_dt < datetime.now() - timedelta(seconds=90):
            now_str = datetime.now().isoformat(timespec="seconds")
            return (
                "拒绝创建：run_at 相对本机当前时间已经是过去时间，任务会马上被当成到期并触发。"
                f" 你填的是 {run_norm}，当前约 {now_str}。"
                " 如果本意是今天下午三点之类的，请检查年份是否写错。"
            )

        repeat = (tool_input.get("repeat") or "once").strip().lower()
        repeat_alias = {
            "每天": "daily",
            "每日": "daily",
            "每周": "weekly",
            "每月": "monthly",
            "每年": "yearly",
            "everyday": "daily",
        }
        repeat = repeat_alias.get(repeat, repeat)
        if repeat not in ("once", "daily", "weekly", "monthly", "yearly", "interval"):
            return f"不支持的 repeat：{repeat}，请用 once / daily / weekly / monthly / yearly / interval"

        interval_seconds = tool_input.get("interval_seconds")
        if repeat == "interval":
            try:
                interval_seconds = int(interval_seconds)
            except (TypeError, ValueError):
                return "repeat 为 interval 时必须提供正整数 interval_seconds（秒）"
            if interval_seconds < 60:
                return "interval_seconds 最少 60 秒"
            if interval_seconds > 86400 * 7:
                return "interval_seconds 最多 7 天（604800 秒）"
        else:
            interval_seconds = None

        if count_scheduled_tasks(session_id) >= MAX_SCHEDULED_TASKS_PER_SESSION:
            return (
                f"当前会话定时任务已达上限（{MAX_SCHEDULED_TASKS_PER_SESSION} 条），"
                "请先 list 再 cancel 掉几条"
            )

        title = (tool_input.get("title") or "提醒").strip()[:80] or "提醒"
        task_id = create_scheduled_task(
            session_id,
            title,
            instruction,
            run_norm,
            repeat,
            interval_seconds,
        )
        interval_text = f"，间隔 {interval_seconds} 秒" if repeat == "interval" else ""
        return (
            f"已创建定时任务 id={task_id}，将在 {run_norm} 首次触发（repeat={repeat}{interval_text}）"
        )

    return "未知 action，请用 create、list 或 cancel"


def _handle_manage_scheduled_task(tool_input: dict, context: ToolContext) -> str:
    return manage_scheduled_task(context.session_id, tool_input)


SCHEDULER_SERVER = BuiltinToolServer(
    name="scheduler",
    description="Reminder and scheduled task tools.",
    tools=(
        ToolSpec(
            server="scheduler",
            name="manage_scheduled_task",
            description=(
                "为当前会话创建、列出或取消定时任务。到点后系统会再以你的身份跑一轮对话："
                "你会收到一条以“定时任务”开头的系统提示，像平时一样自然回复用户，回复会真的发给对方。"
                "run_at 请使用本地时间 ISO 8601，例如 2026-04-26T15:30:00。"
                "repeat 支持 once、daily、weekly、monthly、yearly、interval。"
                "用户明确表示某个提醒不用了、已经完成、或现在就去做了时，可以用 cancel_matching 和 query 取消匹配提醒。"
                "用户改变已有提醒时间时，用 reschedule_matching、query 和新的 run_at 更新匹配提醒，不要重复创建。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create | list | cancel | cancel_matching | reschedule_matching",
                    },
                    "title": {
                        "type": "string",
                        "description": "create 时可选的短标题，默认是“提醒”",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "create 时必填：到点后要说什么、提醒什么、或做什么",
                    },
                    "run_at": {
                        "type": "string",
                        "description": "create 时必填：首次触发的本地时间 ISO 字符串",
                    },
                    "repeat": {
                        "type": "string",
                        "description": "once | daily | weekly | monthly | yearly | interval",
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "description": "repeat 为 interval 时必填，两次触发之间的秒数",
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "cancel 时可填，要取消的真实任务 id；优先于 task_index",
                    },
                    "task_index": {
                        "type": "integer",
                        "description": "cancel 时可填，来自 list 输出的 #序号；如果同时给 task_id，优先使用 task_id",
                    },
                    "query": {
                        "type": "string",
                        "description": "cancel_matching / reschedule_matching 时必填，用自然语言描述要匹配的提醒，例如 睡觉提醒、早起提醒",
                    },
                },
                "required": ["action"],
            },
            handler=_handle_manage_scheduled_task,
            legacy_names=("manage_scheduled_task",),
        ),
    ),
)
