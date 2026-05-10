"""Adapter-agnostic slash commands."""

from __future__ import annotations

import asyncio
import os

from nonebot import on_command
from nonebot.adapters import Event, Message
from nonebot.params import CommandArg

import httpx

from pupu.config import load_arbiter_base_url, load_arbiter_timeout_seconds

from pupu.facts_report import format_facts_report
from pupu.important_event_report import format_important_events_report
from pupu.familiarity import PROACTIVE_THRESHOLD, get_proactive_freq
from pupu.llm import (
    SUPPORTED_PROVIDERS,
    ProviderError,
    get_provider_name,
    set_provider_name,
)
from pupu.memory import (
    get_familiarity_info,
    get_recent_messages,
    reset_session,
    get_familiarity,
)
from pupu.maintenance import run_memory_maintenance
from pupu.proactive import (
    _get_current_period,
    _had_recent_chat_within,
    _is_quiet_hours,
    _minutes_since_last_chat,
    _model_should_proactively_reach_out,
    generate_proactive_message,
)
from pupu.tools import manage_scheduled_task
from pupu.tts import get_tts_config, get_tts_status

from .common import is_owner, resolve_sessions
from . import state

HELP_TEXT = """PuPu 可用命令
/help：查看这份帮助
/score：查看好感度
/tasks：查看定时任务
/important：查看重要事件记忆
/facts：查看长期事实记忆
/history：查看最近聊天记录
/voice on：开启语音回复
/voice off：关闭语音回复
/voice status：查看语音回复状态
/provider：查看当前模型源
/provider xiaoshuoai：切换聊天模型源（管理员）
/provider deepseek：切换聊天模型源（管理员）
/proactive：手动触发一次主动消息检查（管理员）
/reset：重置当前会话记忆（管理员）
/tidy：手动整理长期记忆和定时任务（管理员）
/silence on：本群仲裁强制不接话（管理员，需中心化仲裁服务）
/silence off：恢复本群仲裁接话
/silence：查看本群静默状态
"""

help_cmd = on_command(
    "help",
    aliases={"commands", "帮助", "命令", "指令"},
    priority=5,
    block=True,
)
score_cmd = on_command("score", priority=5, block=True)
tasks_cmd = on_command("tasks", aliases={"定时任务"}, priority=5, block=True)
important_cmd = on_command(
    "important",
    aliases={"events", "important_events", "重要事件", "记忆事件"},
    priority=5,
    block=True,
)
facts_cmd = on_command(
    "facts",
    aliases={"fact", "memory_facts", "长期记忆", "事实记忆"},
    priority=5,
    block=True,
)
history_cmd = on_command("history", priority=5, block=True)
reset_cmd = on_command("reset", priority=5, block=True)
tidy_cmd = on_command(
    "tidy",
    aliases={"cleanup", "整理记忆", "整理"},
    priority=5,
    block=True,
)
voice_cmd = on_command(
    "voice",
    aliases={"tts", "语音", "语音回复"},
    priority=5,
    block=True,
)
proactive_cmd = on_command(
    "proactive",
    aliases={"主动", "主动消息"},
    priority=5,
    block=True,
)
provider_cmd = on_command(
    "provider",
    aliases={"llm", "模型源", "模型"},
    priority=5,
    block=True,
)
silence_cmd = on_command(
    "silence",
    aliases={"silenc", "沉默", "静默", "仲裁静默"},
    priority=5,
    block=True,
)


@help_cmd.handle()
async def handle_help():
    await help_cmd.finish(HELP_TEXT)


@score_cmd.handle()
async def handle_score(event: Event):
    _context_sid, identity_sid = resolve_sessions(event)
    info = get_familiarity_info(identity_sid)
    text = f"好感度: {info['score']}/100\n等级: {info['level']}"
    await score_cmd.finish(text)


@tasks_cmd.handle()
async def handle_tasks(event: Event):
    context_sid, _identity_sid = resolve_sessions(event)
    await tasks_cmd.finish(manage_scheduled_task(context_sid, {"action": "list"}))


@important_cmd.handle()
async def handle_important(event: Event):
    _context_sid, identity_sid = resolve_sessions(event)
    await important_cmd.finish(format_important_events_report(identity_sid))


@facts_cmd.handle()
async def handle_facts(event: Event):
    _context_sid, identity_sid = resolve_sessions(event)
    await facts_cmd.finish(format_facts_report(identity_sid))


@history_cmd.handle()
async def handle_history(event: Event):
    context_sid, _identity_sid = resolve_sessions(event)
    messages = get_recent_messages(10, context_sid)
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
    context_sid, identity_sid = resolve_sessions(event)
    reset_session(context_sid)
    if identity_sid != context_sid:
        reset_session(identity_sid)
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


def _voice_config_warning() -> str:
    cfg = get_tts_config()
    status = get_tts_status(cfg)
    if not cfg.enabled:
        return "\n但 PUPU_TTS_ENABLED 还没开启，所以当前仍只会发文字。"
    if status.reason == "provider_missing":
        return "\n但 PUPU_TTS_PROVIDER 还没配置，所以当前仍只会发文字。"
    if status.reason == "provider_unavailable":
        return f"\n但当前 provider `{cfg.provider}` 还没安装接入，所以当前仍只会发文字。"
    return ""


@voice_cmd.handle()
async def handle_voice(event: Event, args: Message = CommandArg()):
    user_id = event.get_user_id()
    if not is_owner(user_id):
        await voice_cmd.finish("只有管理员才能切换语音回复。")

    action = args.extract_plain_text().strip().lower()
    if action in {"on", "enable", "start", "open", "1", "开启", "打开", "开"}:
        state.tts_reply_enabled = True
        await voice_cmd.finish("语音回复已开启。之后会先发文字，再追加一条语音。" + _voice_config_warning())
    if action in {"off", "disable", "stop", "close", "0", "关闭", "关"}:
        state.tts_reply_enabled = False
        await voice_cmd.finish("语音回复已关闭。之后只发文字。")

    cfg = get_tts_config()
    status = get_tts_status(cfg)
    switch = "开启" if state.tts_reply_enabled else "关闭"
    config = "就绪" if status.ready else "未就绪"
    provider = cfg.provider or "未配置"
    installed = ", ".join(status.installed_providers) if status.installed_providers else "无"
    reason_map = {
        "disabled": "TTS 总开关未启用",
        "provider_missing": "还没有配置 provider",
        "provider_unavailable": "provider 还没接入到项目里",
        "ok": "可正常尝试语音合成",
    }
    reason = reason_map.get(status.reason, status.reason)
    await voice_cmd.finish(
        "语音回复："
        + switch
        + "\nTTS 配置："
        + config
        + "\n当前 provider："
        + provider
        + "\n已安装 provider："
        + installed
        + "\n状态说明："
        + reason
        + "\n用法：/voice on 或 /voice off"
    )


@proactive_cmd.handle()
async def handle_proactive(event: Event, args: Message = CommandArg()):
    user_id = event.get_user_id()
    if not is_owner(user_id):
        await proactive_cmd.finish("只有管理员才能手动触发主动消息。")

    force = args.extract_plain_text().strip().lower() in {"force", "now", "run", "强制", "立即"}
    score = get_familiarity(state.OWNER_SESSION)
    freq = get_proactive_freq(score)
    period = _get_current_period()
    idle_minutes = _minutes_since_last_chat()

    print(
        f"[pupu][proactive] command start force={force} score={score} "
        f"freq={freq} period={period['name'] if period else None} idle_minutes={idle_minutes}"
    )

    if not force:
        if freq is None:
            print("[pupu][proactive] command skip=no_proactive_frequency")
            await proactive_cmd.finish("当前好感度还没到主动消息频率范围。")
        if _is_quiet_hours():
            print("[pupu][proactive] command skip=quiet_hours")
            await proactive_cmd.finish("现在是静默时段，先不主动打扰。")
        if _had_recent_chat_within(60):
            print("[pupu][proactive] command skip=recent_chat_within_60min")
            await proactive_cmd.finish("最近 60 分钟内刚聊过，先不主动打扰。")
        if period is None:
            print("[pupu][proactive] command skip=no_current_period")
            await proactive_cmd.finish("当前时段不在主动消息范围内。")
        if score < PROACTIVE_THRESHOLD:
            print(
                f"[pupu][proactive] command skip=low_score score={score} threshold={PROACTIVE_THRESHOLD}"
            )
            await proactive_cmd.finish("当前好感度还没到主动消息门槛。")

    if period is None:
        period = _get_current_period()
    if period is None:
        await proactive_cmd.finish("当前没有可用的时段定义。")

    should_send = await asyncio.to_thread(
        _model_should_proactively_reach_out,
        score,
        period,
        idle_minutes,
    )
    print(f"[pupu][proactive] command judge_result should_send={should_send}")
    if not should_send:
        await proactive_cmd.finish("模型判断这次不需要主动找你。")

    text = await asyncio.to_thread(generate_proactive_message, score, period)
    if not text:
        await proactive_cmd.finish("主动消息生成失败。")

    print(f"[pupu][proactive] command generate_done text={text[:120]}")
    await proactive_cmd.finish(text)


_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "codex": "codex_cli",
    "codex_cli": "codex_cli",
    "codex-cli": "codex_cli",
    "xiaoshuo": "xiaoshuoai",
    "xiaoshuoai": "xiaoshuoai",
    "novel": "xiaoshuoai",
    "gpt4novel": "xiaoshuoai",
    "小说": "xiaoshuoai",
    "小说ai": "xiaoshuoai",
    "deepseek": "deepseek",
    "ds": "deepseek",
}

_ROLE_ALIASES = {
    "chat": "chat",
    "聊天": "chat",
    "reply": "chat",
    "judge": "judge",
    "review": "judge",
    "记忆": "judge",
    "整理": "maintenance",
    "maintenance": "maintenance",
    "proactive": "proactive",
    "主动": "proactive",
}


def _provider_status_text() -> str:
    rows = [
        f"聊天：{get_provider_name('chat')}",
        f"记忆整理：{get_provider_name('judge')}",
        f"维护：{get_provider_name('maintenance')}",
        f"主动消息：{get_provider_name('proactive')}",
        "可选：" + ", ".join(SUPPORTED_PROVIDERS),
        "用法：/provider deepseek 或 /provider chat codex_cli",
    ]
    return "\n".join(rows)


def _normalize_provider(raw: str) -> str:
    return _PROVIDER_ALIASES.get(raw.strip().lower(), raw.strip().lower())


def _normalize_role(raw: str) -> str | None:
    return _ROLE_ALIASES.get(raw.strip().lower())


@provider_cmd.handle()
async def handle_provider(event: Event, args: Message = CommandArg()):
    action = args.extract_plain_text().strip()
    action_lower = action.lower()
    if not action or action_lower in {"status", "list", "当前", "状态"}:
        await provider_cmd.finish(_provider_status_text())

    user_id = event.get_user_id()
    if not is_owner(user_id):
        await provider_cmd.finish("只有管理员才能切换模型源。")

    parts = action.split()
    role = "chat"
    provider = ""
    if len(parts) == 1:
        provider = _normalize_provider(parts[0])
    elif parts[0].lower() in {"set", "use", "切换"} and len(parts) >= 2:
        provider = _normalize_provider(parts[1])
    elif parts[0].lower() in {"all", "全部"} and len(parts) >= 2:
        role = "all"
        provider = _normalize_provider(parts[1])
    else:
        normalized_role = _normalize_role(parts[0])
        if not normalized_role:
            await provider_cmd.finish("用法：/provider deepseek 或 /provider chat codex_cli")
        role = normalized_role
        provider = _normalize_provider(parts[1])

    roles = ("chat", "judge", "maintenance", "proactive") if role == "all" else (role,)
    try:
        for item in roles:
            set_provider_name(item, provider)
    except ProviderError as exc:
        await provider_cmd.finish(str(exc))

    warning = ""
    if provider == "xiaoshuoai" and not os.environ.get("PUPU_XIAOSHUOAI_API_KEY", "").strip():
        warning = "\n但 PUPU_XIAOSHUOAI_API_KEY 还没配置，请先填好 .env。"
    if provider == "deepseek" and not os.environ.get("PUPU_DEEPSEEK_API_KEY", "").strip():
        warning = "\n但 PUPU_DEEPSEEK_API_KEY 还没配置，请先填好 .env。"
    await provider_cmd.finish(
        f"已切换 {('全部' if role == 'all' else role)} provider 为 {provider}。"
        "\n下一次模型请求生效，重启后会回到 .env 配置。"
        + warning
    )


def _silence_http_timeout() -> float:
    return min(30.0, float(load_arbiter_timeout_seconds()))


@silence_cmd.handle()
async def handle_silence(event: Event, args: Message = CommandArg()):
    user_id = event.get_user_id()
    if not is_owner(user_id):
        await silence_cmd.finish("只有管理员才能切换群仲裁静默。")
    gid = getattr(event, "group_id", None)
    if gid is None:
        await silence_cmd.finish("这个命令只在群里用。")
    group_id = str(gid).strip()
    if not group_id:
        await silence_cmd.finish("无法识别群号。")

    base = load_arbiter_base_url().rstrip("/")
    url = f"{base}/api/group_silence"
    timeout = _silence_http_timeout()
    action = args.extract_plain_text().strip().lower()

    if action in {"", "status", "状态", "?"}:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params={"group_id": group_id})
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            await silence_cmd.finish(f"查询失败（请确认仲裁服务已启动）：{exc}")
        if not data.get("ok"):
            await silence_cmd.finish(f"查询失败：{data.get('error', data)}")
        on = bool(data.get("enabled"))
        await silence_cmd.finish(
            "本群仲裁静默：" + ("已开启（强制不接话）" if on else "已关闭（正常接话）")
            + "\n用法：/silence on | /silence off"
        )

    if action in {"on", "enable", "start", "open", "1", "开启", "打开", "开", "true", "yes"}:
        enabled = True
    elif action in {"off", "disable", "stop", "close", "0", "关闭", "关", "false", "no"}:
        enabled = False
    else:
        await silence_cmd.finish("用法：/silence on 关闭接话，/silence off 恢复，/silence 查看状态")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"group_id": group_id, "enabled": enabled})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        await silence_cmd.finish(f"同步失败（请确认仲裁服务已启动）：{exc}")
    if not data.get("ok"):
        await silence_cmd.finish(f"失败：{data.get('error', data)}")

    if enabled:
        await silence_cmd.finish("已开启：本群仲裁将固定为不接话（speaker 恒为 none），直到 /silence off。")
    else:
        await silence_cmd.finish("已关闭：本群仲裁恢复正常接话。")
