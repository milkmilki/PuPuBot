"""Adapter-agnostic slash commands."""

from __future__ import annotations

import asyncio

from nonebot import on_command
from nonebot.adapters import Event

from pupu.memory import (
    get_event_log,
    get_familiarity_info,
    get_recent_messages,
    reset_session,
)
from pupu.maintenance import run_memory_maintenance
from pupu.tools import manage_scheduled_task

from .common import is_owner, resolve_session

score_cmd = on_command("score", priority=5, block=True)
tasks_cmd = on_command("tasks", aliases={"定时任务"}, priority=5, block=True)
history_cmd = on_command("history", priority=5, block=True)
reset_cmd = on_command("reset", priority=5, block=True)
tidy_cmd = on_command(
    "tidy",
    aliases={"cleanup", "整理记忆", "整理"},
    priority=5,
    block=True,
)


@score_cmd.handle()
async def handle_score(event: Event):
    sid = resolve_session(event)
    info = get_familiarity_info(sid)
    events = get_event_log(5, sid)
    text = f"好感度: {info['score']}/100\n等级: {info['level']}"
    if events:
        text += "\n\n最近事件:"
        for item in events:
            sign = "+" if item["delta"] > 0 else ""
            text += (
                f"\n{item['date'][:10]} [{sign}{item['delta']}] "
                f"{item['description']}"
            )
    await score_cmd.finish(text)


@tasks_cmd.handle()
async def handle_tasks(event: Event):
    sid = resolve_session(event)
    await tasks_cmd.finish(manage_scheduled_task(sid, {"action": "list"}))


@history_cmd.handle()
async def handle_history(event: Event):
    sid = resolve_session(event)
    messages = get_recent_messages(10, sid)
    if not messages:
        await history_cmd.finish("还没有聊天记录。")
    lines = []
    for message in messages:
        prefix = "你" if message["role"] == "user" else "仆仆"
        lines.append(f"{prefix}: {message['content'][:80]}")
    await history_cmd.finish("\n".join(lines))


@reset_cmd.handle()
async def handle_reset(event: Event):
    user_id = event.get_user_id()
    if not is_owner(user_id):
        await reset_cmd.finish("只有管理员才能重置。")
    sid = resolve_session(event)
    reset_session(sid)
    await reset_cmd.finish("已重置。仆仆回到了最初的状态。")


@tidy_cmd.handle()
async def handle_tidy(event: Event):
    user_id = event.get_user_id()
    if not is_owner(user_id):
        await tidy_cmd.finish("只有管理员才能整理长期记忆和定时任务")
    report = await asyncio.to_thread(
        run_memory_maintenance,
        "manual",
        True,
    )
    await tidy_cmd.finish(report)
