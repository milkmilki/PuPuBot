"""Surface-neutral slash command execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from .command_registry import command_usage, render_help, resolve_command
from .config import load_arbiter_base_url, load_arbiter_timeout_seconds
from .event_thread_report import format_event_threads_report
from .facts_report import format_facts_report
from .logging_utils import is_debug_console_enabled, set_debug_console_enabled
from .memory import get_familiarity_info, get_recent_messages, reset_session
from .memory_index import (
    clear_memu_session,
    format_memu_recall_report,
    run_memu_maintenance,
)
from .proactive_control import is_proactive_enabled, set_proactive_enabled
from .sessions import OWNER_SESSION
from .tools import manage_scheduled_task

TIDY_USAGE = f"用法：{command_usage('tidy')}"


@dataclass(frozen=True, slots=True)
class CommandContext:
    surface: str
    context_session: str
    identity_session: str
    is_admin: bool = False
    user_id: str = ""
    group_id: str = ""
    can_exit: bool = False


@dataclass(frozen=True, slots=True)
class CommandResult:
    handled: bool
    text: str = ""
    should_exit: bool = False


def is_command_text(text: str) -> bool:
    return bool(str(text or "").lstrip().startswith("/"))


def _parse_tidy_mode(command_arg: str) -> tuple[str | None, str | None]:
    mode = command_arg.strip().lower()
    if not mode:
        return "apply", None
    if mode in {"check", "apply", "rebuild"}:
        return mode, None
    return None, TIDY_USAGE


def _format_history(session_id: str, *, assistant_name: str = "PuPu") -> str:
    messages = get_recent_messages(20, session_id)
    if not messages:
        return "还没有聊天记录。"
    lines = []
    for message in messages:
        role = str(message.get("role") or "")
        prefix = "你" if role == "user" else assistant_name
        content = str(message.get("content") or "")
        lines.append(f"{prefix}: {content[:120]}")
    return "\n".join(lines)


def _parse_on_off(action: str) -> bool | None:
    raw = str(action or "").strip().lower()
    if raw in {"on", "enable", "enabled", "open", "start", "1", "true", "yes", "开启", "打开", "开"}:
        return True
    if raw in {"off", "disable", "disabled", "close", "stop", "0", "false", "no", "关闭", "关"}:
        return False
    return None


def _silence_timeout() -> float:
    return min(30.0, float(load_arbiter_timeout_seconds()))


async def _get_group_silence(group_id: str) -> tuple[bool | None, str]:
    base = load_arbiter_base_url().rstrip("/")
    url = f"{base}/api/group_silence"
    try:
        async with httpx.AsyncClient(timeout=_silence_timeout()) as client:
            response = await client.get(url, params={"group_id": group_id})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return None, f"仲裁服务查询失败：{exc}"
    if not data.get("ok"):
        return None, f"查询失败：{data.get('error', data)}"
    return bool(data.get("enabled")), ""


async def _set_group_silence(group_id: str, enabled: bool) -> str:
    base = load_arbiter_base_url().rstrip("/")
    url = f"{base}/api/group_silence"
    try:
        async with httpx.AsyncClient(timeout=_silence_timeout()) as client:
            response = await client.post(url, json={"group_id": group_id, "enabled": enabled})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return f"\n（仲裁服务暂不可用，仅本地状态已生效：{exc}）"
    if not data.get("ok"):
        return f"\n（仲裁服务同步失败，仅本地状态已生效：{data.get('error', data)}）"
    return ""


async def execute_command(
    raw_text: str,
    context: CommandContext,
    *,
    silence_getter=None,
    silence_setter=None,
    proactive_starter=None,
    proactive_stopper=None,
) -> CommandResult:
    text = str(raw_text or "").strip()
    if not is_command_text(text):
        return CommandResult(False)

    command_name, _, command_arg = text.partition(" ")
    spec = resolve_command(command_name, surface=context.surface)
    if spec is None:
        return CommandResult(False)

    command_id = spec.command_id
    if context.surface in spec.admin_surfaces and not context.is_admin:
        return CommandResult(True, "只有管理员才能使用这个命令。")

    if command_id == "help":
        return CommandResult(True, render_help(surface=context.surface))
    if command_id == "quit":
        if context.can_exit:
            return CommandResult(True, "再见。", should_exit=True)
        return CommandResult(True, "这个入口不支持退出命令。")
    if command_id == "score":
        info = get_familiarity_info(context.identity_session)
        return CommandResult(
            True,
            f"好感度: {info['score']}/100\n等级: {info['level']}\n上次更新: {info['updated_at'][:10]}",
        )
    if command_id == "history":
        return CommandResult(True, _format_history(context.context_session))
    if command_id == "tasks":
        return CommandResult(
            True,
            await asyncio.to_thread(
                manage_scheduled_task,
                context.context_session,
                {"action": "list"},
            ),
        )
    if command_id == "events":
        return CommandResult(
            True,
            await asyncio.to_thread(
                format_event_threads_report,
                context.identity_session,
                query=command_arg,
            ),
        )
    if command_id == "facts":
        return CommandResult(
            True,
            await asyncio.to_thread(
                format_facts_report,
                context.identity_session,
                command_arg,
            ),
        )
    if command_id == "recall":
        query = command_arg.strip()
        if not query:
            return CommandResult(True, "用法：/recall <内容>")
        return CommandResult(
            True,
            await asyncio.to_thread(
                format_memu_recall_report,
                query,
                context.identity_session,
                context.context_session,
            ),
        )
    if command_id == "tidy":
        tidy_mode, tidy_usage = _parse_tidy_mode(command_arg)
        if tidy_usage:
            return CommandResult(True, tidy_usage)
        return CommandResult(
            True,
            await asyncio.to_thread(
                run_memu_maintenance,
                OWNER_SESSION,
                mode=tidy_mode,
            ),
        )
    if command_id == "reset":
        reset_session(context.context_session)
        await asyncio.to_thread(clear_memu_session, context.context_session)
        if context.identity_session != context.context_session:
            reset_session(context.identity_session)
            await asyncio.to_thread(clear_memu_session, context.identity_session)
        return CommandResult(True, "已重置当前会话记忆、好感度和聊天记录。")
    if command_id == "proactive":
        action = command_arg.strip().lower()
        if action in {"", "status", "状态", "?"}:
            return CommandResult(
                True,
                "主动消息："
                + ("已开启" if is_proactive_enabled() else "已关闭"),
            )
        enabled = _parse_on_off(action)
        if enabled is True:
            set_proactive_enabled(True)
            if proactive_starter is not None:
                msg = proactive_starter()
                if asyncio.iscoroutine(msg):
                    msg = await msg
                return CommandResult(True, str(msg or "主动消息已开启。"))
            return CommandResult(True, "主动消息已开启。")
        if enabled is False:
            set_proactive_enabled(False)
            if proactive_stopper is not None:
                msg = proactive_stopper()
                if asyncio.iscoroutine(msg):
                    msg = await msg
                return CommandResult(True, str(msg or "主动消息已关闭。"))
            return CommandResult(True, "主动消息已关闭。")
        return CommandResult(True, "用法：/proactive [status|on|off]")
    if command_id == "debug":
        action = command_arg.strip().lower()
        if action in {"", "status", "状态", "?"}:
            return CommandResult(
                True,
                "调试日志：" + ("已开启" if is_debug_console_enabled() else "已关闭"),
            )
        enabled = _parse_on_off(action)
        if enabled is None:
            return CommandResult(True, "用法：/debug [status|on|off]")
        set_debug_console_enabled(enabled)
        return CommandResult(True, "调试日志已" + ("开启。" if enabled else "关闭。"))
    if command_id == "silence":
        if not context.group_id:
            return CommandResult(True, "这个命令只在群里可用。")
        group_id = context.group_id
        action = command_arg.strip().lower()
        local_on = bool(silence_getter(group_id)) if silence_getter is not None else False
        if action in {"", "status", "状态", "?"}:
            remote, note = await _get_group_silence(group_id)
            if remote is None:
                return CommandResult(
                    True,
                    "本群仲裁静默："
                    + ("已开启（本地强制不接话）" if local_on else "未知（仲裁服务不可用，本地未静默）")
                    + f"\n{note}"
                    + "\n用法：/silence on | /silence off",
                )
            on = local_on or bool(remote)
            return CommandResult(
                True,
                "本群仲裁静默："
                + ("已开启（不接话）" if on else "已关闭（正常接话）")
                + "\n用法：/silence on | /silence off",
            )
        enabled = _parse_on_off(action)
        if enabled is None:
            return CommandResult(True, "用法：/silence on 关闭接话；/silence off 恢复；/silence 查看状态")
        if silence_setter is not None:
            silence_setter(group_id, enabled)
        sync_note = await _set_group_silence(group_id, enabled)
        if enabled:
            return CommandResult(
                True,
                "已开启：本群本地静默，不再连接仲裁服务，也不会接话，直到 /silence off。"
                + sync_note,
            )
        return CommandResult(True, "已关闭：本群恢复连接仲裁服务并正常接话。" + sync_note)

    return CommandResult(False)

