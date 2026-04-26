"""NoneBot plugin entry for message handling, debounce, commands, and proactive tasks."""

import asyncio
import json
import random
from datetime import datetime

from nonebot import on_command, on_message, get_driver
from nonebot.rule import to_me
from nonebot.adapters import Event

from pupu.agent import chat
from pupu.tools import manage_scheduled_task
from pupu.familiarity import compute_reply_delay, PROACTIVE_THRESHOLD
from pupu.memory import get_event_log, get_familiarity, get_familiarity_info, get_last_user_message_time, get_recent_messages, init_db, reset_session
from pupu.proactive import proactive_loop
from pupu.scheduler import onebot_scheduled_tasks_loop
from pupu.richmsg import parse_onebot_message, parse_qq_official_message

init_db()

OWNER_SESSION = "owner"

def _load_owner_ids() -> set[str]:
    try:
        with open("config.json", encoding="utf-8") as f:
            cfg = json.load(f)
        return {str(x) for x in cfg.get("owner_ids", [])}
    except Exception:
        return set()

def _is_owner(user_id) -> bool:
    return str(user_id) in _load_owner_ids()

_is_admin = _is_owner


def _compute_reply_speed_hint(session_id: str) -> str | None:
    """Compute a human-readable hint about how fast the user replied."""
    last_ts = get_last_user_message_time(session_id)
    if not last_ts:
        return None
    try:
        last_dt = datetime.fromisoformat(last_ts)
        delta = (datetime.now() - last_dt).total_seconds()
        if delta < 10:
            return "用户刚刚秒回了你，回复非常快"
        if delta < 120:
            return "用户回复速度正常"
        if delta < 600:
            return "用户隔了好几分钟才回复，可能在忙别的"
        return "用户隔了很久才回复，可能刚回来"
    except Exception:
        return None


def _split_message(text: str) -> list[str]:
    """Split reply on newlines — each line becomes a separate QQ message."""
    text = text.strip()
    if not text:
        return [text]
    parts = [line.strip() for line in text.split('\n') if line.strip()]
    return parts if parts else [text]


async def _send_segments(bot, event, segments: list[str], prefix=None):
    """Send message segments with typing-like delays between them."""
    for i, seg in enumerate(segments):
        if i == 0 and prefix is not None:
            await bot.send(event, prefix + seg)
        else:
            await bot.send(event, seg)
        if i < len(segments) - 1:
            # Simulate typing time: ~0.5-1.5s per 10 chars, min 0.8s, max 4s
            typing_time = min(4, max(0.8, len(segments[i + 1]) * random.uniform(0.05, 0.15)))
            await asyncio.sleep(typing_time)


async def _send_private_segments(bot, user_id: int, segments: list[str]):
    """Send private-message segments with typing-like delays between them."""
    for i, seg in enumerate(segments):
        await bot.send_private_msg(user_id=user_id, message=seg)
        if i < len(segments) - 1:
            typing_time = min(4, max(0.8, len(segments[i + 1]) * random.uniform(0.05, 0.15)))
            await asyncio.sleep(typing_time)


async def _owner_no_reply_followup(bot, owner_qq: int):
    """If owner doesn't reply for 10 minutes, remind Pupu internally and send her generated follow-up."""
    try:
        await asyncio.sleep(600)
        synthetic = "[系统提醒] 你10分钟前主动找对方聊天了，但对方一直没回。请你自然地发一条轻微撒娇但不过分黏人的消息。"
        hint = "这是未回复提醒触发的跟进消息，语气轻一点，不要重复之前那句。"
        text = await asyncio.to_thread(
            chat,
            synthetic,
            OWNER_SESSION,
            True,
            None,
            hint,
        )
        if not text or not str(text).strip():
            return
        followup = str(text).strip()
        segments = _split_message(followup)
        await _send_private_segments(bot, owner_qq, segments)
        _log("send", "私聊", str(owner_qq), followup)
    except asyncio.CancelledError:
        return


_proactive_task: asyncio.Task | None = None
_scheduler_task: asyncio.Task | None = None
_proactive_followup_task: asyncio.Task | None = None

# Try importing both adapters — only the loaded one will fire events
try:
    from nonebot.adapters.onebot.v11 import (
        Bot as OBBot,
        MessageEvent as OBMessageEvent,
        PrivateMessageEvent as OBPrivateEvent,
        GroupMessageEvent as OBGroupEvent,
        MessageSegment as OBMsgSeg,
    )
    HAS_ONEBOT = True
except ImportError:
    HAS_ONEBOT = False

try:
    from nonebot.adapters.qq import (
        Bot as QQBot,
        MessageCreateEvent,
        C2CMessageCreateEvent,
        GroupAtMessageCreateEvent,
    )
    HAS_QQ_OFFICIAL = True
except ImportError:
    HAS_QQ_OFFICIAL = False


def _log(direction: str, session: str, user: str, text: str):
    """Print message log to console."""
    now = datetime.now().strftime("%H:%M:%S")
    arrow = "<<<" if direction == "recv" else ">>>"
    label = "收到" if direction == "recv" else "发送"
    display = text[:120] + "..." if len(text) > 120 else text
    print(f"[{now}] {arrow} {label} | {session} | {user} | {display}")


# ==================== Message Debounce Buffer ====================

DEBOUNCE_SECONDS = 20.0

_msg_buffers: dict[str, dict] = {}
_debounce_tasks: dict[str, asyncio.Task] = {}
_session_phase: dict[str, str] = {}  # "buffering" | "delaying" | "processing"


async def _buffer_message(sid: str, text: str, image_urls: list[str],
                          bot, event, is_admin: bool,
                          nickname: str, session_label: str,
                          reply_prefix=None):
    """Buffer a message and reset the debounce timer for this session."""
    if sid not in _msg_buffers:
        _msg_buffers[sid] = {
            "texts": [],
            "image_urls": [],
            "bot": bot,
            "event": event,
            "is_admin": is_admin,
            "nickname": nickname,
            "session_label": session_label,
            "reply_prefix": reply_prefix,
        }

    global _proactive_followup_task

    buf = _msg_buffers[sid]

    if sid == OWNER_SESSION and _proactive_followup_task is not None and not _proactive_followup_task.done():
        _proactive_followup_task.cancel()
        _proactive_followup_task = None

    if text:
        buf["texts"].append(text)
    buf["image_urls"].extend(image_urls)
    buf["bot"] = bot
    buf["event"] = event
    if reply_prefix is not None:
        buf["reply_prefix"] = reply_prefix

    phase = _session_phase.get(sid)
    if phase in ("delaying", "processing"):
        return

    if sid in _debounce_tasks:
        _debounce_tasks[sid].cancel()

    _debounce_tasks[sid] = asyncio.create_task(_debounce_flush(sid))


async def _debounce_flush(sid: str):
    """Debounce → delay → re-debounce → process."""
    # Phase 1: initial debounce
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    # Phase 2: compute and apply reply delay
    score = get_familiarity(sid)
    delay, replacement = compute_reply_delay(score)

    if replacement is not None:
        print(f"[pupu] 敷衍回复: {replacement}")
        buf = _msg_buffers.pop(sid, None)
        _debounce_tasks.pop(sid, None)
        _session_phase.pop(sid, None)
        if buf:
            combined = "\n".join(t for t in buf["texts"] if t)
            from pupu.memory import save_message
            if combined:
                save_message("user", combined, sid)
            save_message("assistant", replacement, sid)
            _log("recv", buf["session_label"], buf["nickname"], combined or "[图片]")
            _log("send", buf["session_label"], buf["nickname"], replacement)
            segments = _split_message(replacement)
            await _send_segments(buf["bot"], buf["event"], segments, prefix=buf.get("reply_prefix"))
        return

    if delay > 0:
        _session_phase[sid] = "delaying"
        print(f"[pupu] 延迟回复: {delay:.1f}秒 (好感度{score})")
        await asyncio.sleep(delay)
        # Re-debounce: wait for user to finish if they sent more during delay
        _session_phase[sid] = "buffering"
        await asyncio.sleep(DEBOUNCE_SECONDS)

    # Phase 3: process
    _session_phase[sid] = "processing"
    buf = _msg_buffers.pop(sid, None)
    _debounce_tasks.pop(sid, None)

    if not buf:
        _session_phase.pop(sid, None)
        return

    combined_text = "\n".join(t for t in buf["texts"] if t)
    image_urls = buf["image_urls"]

    if not combined_text and not image_urls:
        _session_phase.pop(sid, None)
        return

    try:
        _log("recv", buf["session_label"], buf["nickname"], combined_text or "[图片]")
        speed_hint = _compute_reply_speed_hint(sid)
        reply = await asyncio.to_thread(
            chat, combined_text, sid, buf["is_admin"], image_urls or None, speed_hint
        )

        _log("send", buf["session_label"], buf["nickname"], reply)
        segments = _split_message(reply)
        await _send_segments(buf["bot"], buf["event"], segments, prefix=buf.get("reply_prefix"))
    except Exception as e:
        print(f"[pupu] flush error ({sid}): {e}")
        try:
            await buf["bot"].send(buf["event"], "呃，脑子卡了一下")
        except Exception:
            pass
    finally:
        _session_phase.pop(sid, None)


# ==================== Slash Commands (adapter-agnostic) ====================

score_cmd = on_command("score", priority=5, block=True)


@score_cmd.handle()
async def handle_score(event: Event):
    sid = _resolve_session(event)
    info = get_familiarity_info(sid)
    events = get_event_log(5, sid)
    text = f"好感度: {info['score']}/100\n等级: {info['level']}"
    if events:
        text += "\n\n最近事件:"
        for e in events:
            sign = "+" if e["delta"] > 0 else ""
            text += f"\n{e['date'][:10]} [{sign}{e['delta']}] {e['description']}"
    await score_cmd.finish(text)


tasks_cmd = on_command("tasks", aliases={"定时任务"}, priority=5, block=True)


@tasks_cmd.handle()
async def handle_tasks(event: Event):
    sid = _resolve_session(event)
    await tasks_cmd.finish(manage_scheduled_task(sid, {"action": "list"}))


history_cmd = on_command("history", priority=5, block=True)


@history_cmd.handle()
async def handle_history(event: Event):
    sid = _resolve_session(event)
    messages = get_recent_messages(10, sid)
    if not messages:
        await history_cmd.finish("还没有聊天记录。")
    lines = []
    for m in messages:
        prefix = "你" if m["role"] == "user" else "仆仆"
        lines.append(f"{prefix}: {m['content'][:80]}")
    await history_cmd.finish("\n".join(lines))


reset_cmd = on_command("reset", priority=5, block=True)


@reset_cmd.handle()
async def handle_reset(event: Event):
    user_id = event.get_user_id()
    if not _is_owner(user_id):
        await reset_cmd.finish("只有管理员才能重置。")
    sid = _resolve_session(event)
    reset_session(sid)
    await reset_cmd.finish("已重置。仆仆回到了最初的状态。")


# ==================== OneBot v11 (NapCat) handlers ====================

if HAS_ONEBOT:
    ob_private = on_message(priority=10, block=True)

    @ob_private.handle()
    async def handle_ob_private(bot: OBBot, event: OBPrivateEvent):
        text, image_urls = parse_onebot_message(event.get_message())
        if not text and not image_urls:
            return
        sid = OWNER_SESSION if _is_owner(event.user_id) else f"private_{event.user_id}"
        nickname = event.sender.nickname or str(event.user_id)
        await _buffer_message(sid, text, image_urls, bot, event,
                              _is_admin(event.user_id), nickname, "私聊")

    ob_group = on_message(rule=to_me(), priority=10, block=True)

    @ob_group.handle()
    async def handle_ob_group(bot: OBBot, event: OBGroupEvent):
        text, image_urls = parse_onebot_message(event.get_message())
        if not text and not image_urls:
            return
        sid = f"group_{event.group_id}"
        nickname = event.sender.nickname or str(event.user_id)
        await _buffer_message(sid, text, image_urls, bot, event,
                              _is_admin(event.user_id), nickname,
                              f"群{event.group_id}",
                              reply_prefix=OBMsgSeg.at(event.user_id) + " ")

    # Proactive messaging: start/stop on bot connect/disconnect
    driver = get_driver()

    @driver.on_bot_connect
    async def _on_ob_connect(bot):
        global _proactive_task, _scheduler_task, _proactive_followup_task
        if not isinstance(bot, OBBot):
            return

        try:
            info = await bot.get_login_info()
            nickname = info.get("nickname", "unknown")
            uin = info.get("user_id", bot.self_id)
            print()
            print("=" * 40)
            print(f"  NapCat connected!")
            print(f"  Bot QQ: {uin}")
            print(f"  Nickname: {nickname}")
            print("=" * 40)
            print()
        except Exception:
            print(f"[pupu] NapCat connected (bot: {bot.self_id})")

        if _scheduler_task is None or _scheduler_task.done():
            _scheduler_task = asyncio.create_task(onebot_scheduled_tasks_loop(bot))
            print("[pupu] scheduled tasks loop started")

        if _proactive_task is not None:
            return

        owner_qq = None
        for oid in _load_owner_ids():
            if oid.isdigit():
                owner_qq = oid
                break
        if not owner_qq:
            print("[pupu] no numeric owner QQ found, proactive messaging disabled")
            return

        score = get_familiarity(OWNER_SESSION)
        if score >= PROACTIVE_THRESHOLD:
            async def send_to_owner(text: str):
                segments = _split_message(text)
                await _send_private_segments(bot, int(owner_qq), segments)
                _log("send", "私聊", str(owner_qq), text)
                if _proactive_followup_task is not None and not _proactive_followup_task.done():
                    _proactive_followup_task.cancel()
                _proactive_followup_task = asyncio.create_task(
                    _owner_no_reply_followup(bot, int(owner_qq))
                )
            _proactive_task = asyncio.create_task(proactive_loop(send_to_owner))
            print(f"[pupu] proactive messaging enabled (familiarity: {score})")
        else:
            print(f"[pupu] proactive messaging disabled (familiarity: {score} < {PROACTIVE_THRESHOLD})")

    @driver.on_bot_disconnect
    async def _on_ob_disconnect(bot):
        global _proactive_task, _scheduler_task, _proactive_followup_task
        if not isinstance(bot, OBBot):
            return
        print(f"[pupu] NapCat disconnected (bot: {bot.self_id})")
        if _scheduler_task is not None:
            _scheduler_task.cancel()
            _scheduler_task = None
        if _proactive_task is not None:
            _proactive_task.cancel()
            _proactive_task = None
        if _proactive_followup_task is not None:
            _proactive_followup_task.cancel()
            _proactive_followup_task = None


# ==================== QQ Official Bot handlers ====================

if HAS_QQ_OFFICIAL:
    qq_channel = on_message(priority=10, block=True)

    @qq_channel.handle()
    async def handle_qq_channel(bot: QQBot, event: MessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        text, image_urls = parse_qq_official_message(event.get_message(), getattr(event, "attachments", None))
        if not text and not image_urls:
            return
        sid = f"channel_{event.channel_id}"
        user = event.get_user_id()
        await _buffer_message(sid, text, image_urls, bot, event,
                              _is_admin(user), user, f"频道{event.channel_id}")

    qq_c2c = on_message(priority=10, block=True)

    @qq_c2c.handle()
    async def handle_qq_c2c(bot: QQBot, event: C2CMessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        text, image_urls = parse_qq_official_message(event.get_message(), getattr(event, "attachments", None))
        if not text and not image_urls:
            return
        user = event.get_user_id()
        sid = OWNER_SESSION if _is_admin(user) else f"c2c_{user}"
        await _buffer_message(sid, text, image_urls, bot, event,
                              _is_admin(user), user, "私聊")

    qq_group_at = on_message(priority=10, block=True)

    @qq_group_at.handle()
    async def handle_qq_group_at(bot: QQBot, event: GroupAtMessageCreateEvent):
        if not isinstance(bot, QQBot):
            return
        text, image_urls = parse_qq_official_message(event.get_message(), getattr(event, "attachments", None))
        if not text and not image_urls:
            return
        sid = f"qqgroup_{event.group_openid}"
        user = event.get_user_id()
        await _buffer_message(sid, text, image_urls, bot, event,
                              _is_admin(user), user, "QQ群")


# ==================== Helpers ====================

def _resolve_session(event: Event) -> str:
    """Return OWNER_SESSION for owner, else a generic fallback."""
    try:
        user_id = event.get_user_id()
        if _is_owner(user_id):
            return OWNER_SESSION
        return f"session_{event.get_session_id()}"
    except Exception:
        return "default"
