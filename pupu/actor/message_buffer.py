"""Per-actor message buffering and open-group arbiter integration."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from pupu.agent import _format_turn_timestamp, chat
from pupu.config import (
    load_arbiter_base_url,
    load_arbiter_subscribe_timeout_seconds,
    load_arbiter_timeout_seconds,
    load_arbiter_unavailable_probe_seconds,
    load_bot_id,
    load_max_consecutive_bot_turns,
    load_open_group_debounce_seconds,
)
from pupu.dialogue_loop import cancel_wait_timer, is_followup_eligible, register_sender
from pupu.instance_context import activate_instance_context
from pupu.memory import save_message_with_speaker
from pupu.message_sources import CHAT
from pupu.sessions import OWNER_SESSION
from pupu.storage.people import resolve_person_for_prompt

from .types import ActorInboundMessage, ActorOutboundTarget


SendText = Callable[[ActorOutboundTarget, str], Awaitable[None]]
CommandHandler = Callable[[ActorInboundMessage], Awaitable[bool]]


@dataclass(slots=True)
class _Buffer:
    message: ActorInboundMessage
    texts: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    speakers: list[dict] = field(default_factory=list)
    is_open_group: bool = False


def _with_turn_timestamp(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return value
    if value.startswith("[时间:") or value.startswith("[鏃堕棿:"):
        return value
    return f"[时间: {_format_turn_timestamp()}] {value}"


def _session_label(message: ActorInboundMessage) -> str:
    return f"群{message.group_id}" if message.group_id else "私聊"


def _log(direction: str, message: ActorInboundMessage, text: str) -> None:
    from datetime import datetime

    now = datetime.now().strftime("%H:%M:%S")
    arrow = "<<<" if direction == "recv" else ">>>"
    label = "收到" if direction == "recv" else "发送"
    display = text[:120] + "..." if len(text) > 120 else text
    user = message.user_name or message.user_id
    print(f"[{now}] {arrow} {label} | {_session_label(message)} | {user} | {display}")


def _identity_session_for_context(session_id: str, identity_session: str | None = None) -> str:
    if str(session_id or "").startswith("group_"):
        return OWNER_SESSION
    return str(identity_session or session_id)


def _canonical_speaker(message: ActorInboundMessage) -> dict:
    person = resolve_person_for_prompt(
        person_key=message.speaker_key,
        qq_id=message.user_id,
        display_name=message.speaker_name or message.user_name,
        kind="qq" if message.user_id else "user",
    )
    if message.speaker_is_bot:
        person["kind"] = "bot"
    return person


def _speaker_item(message: ActorInboundMessage) -> dict:
    person = _canonical_speaker(message)
    return {
        "person_key": str(person.get("person_key") or message.speaker_key or "").strip(),
        "display_name": str(
            person.get("display_name") or message.speaker_name or message.user_name or message.user_id or ""
        ).strip(),
        "qq_id": str(message.user_id or "").strip(),
        "kind": str(person.get("kind") or ("qq" if message.user_id else "user")),
    }


def _speaker_prefix(message: ActorInboundMessage) -> str:
    label = message.speaker_name or message.user_name or message.user_id
    if message.speaker_is_bot:
        return f"[bot {label}(QQ:{message.user_id})] "
    return f"[{label}(QQ:{message.user_id})] "


def _strip_prefix(text: str) -> str:
    value = str(text or "")
    if "]" in value and value.lstrip().startswith("["):
        return value.split("]", 1)[1].strip()
    return value.strip()


class MessageBuffer:
    def __init__(
        self,
        *,
        send_text: SendText,
        handle_command: CommandHandler,
        log: Callable[[str], None] | None = None,
        bot_qq_getter: Callable[[], str] | None = None,
        debounce_seconds: float = 20.0,
        context=None,
    ) -> None:
        self._send_text = send_text
        self._handle_command = handle_command
        self._log = log or print
        self._bot_qq_getter = bot_qq_getter or (lambda: "")
        self._debounce_seconds = float(debounce_seconds)
        self._context = context
        self._buffers: dict[str, _Buffer] = {}
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._session_phase: dict[str, str] = {}
        self._arbiter_subscribers: dict[str, asyncio.Task] = {}
        self._arbiter_last_decision_id: dict[str, int] = {}
        self._arbiter_failure_count: dict[str, int] = {}
        self._arbiter_unavailable: dict[str, bool] = {}
        self._arbiter_next_probe_at: dict[str, float] = {}
        self._local_silenced_groups: set[str] = set()

    async def stop(self) -> None:
        tasks = [
            *self._debounce_tasks.values(),
            *self._arbiter_subscribers.values(),
        ]
        self._debounce_tasks.clear()
        self._arbiter_subscribers.clear()
        self._buffers.clear()
        self._session_phase.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def is_group_silenced(self, group_id: str) -> bool:
        return str(group_id or "").strip() in self._local_silenced_groups

    def set_group_silence(self, group_id: str, enabled: bool) -> None:
        group_id = str(group_id or "").strip()
        if not group_id:
            return
        sid = f"group_{group_id}"
        if enabled:
            self._local_silenced_groups.add(group_id)
            task = self._arbiter_subscribers.pop(group_id, None)
            if task and not task.done():
                task.cancel()
            self._buffers.pop(sid, None)
            self._session_phase.pop(sid, None)
            self._arbiter_failure_count.pop(group_id, None)
            self._arbiter_unavailable.pop(group_id, None)
            self._arbiter_next_probe_at.pop(group_id, None)
            self._log(f"[pupu][arbiter] local silence on group={group_id}; subscriber stopped")
        else:
            self._local_silenced_groups.discard(group_id)
            self._arbiter_unavailable.pop(group_id, None)
            self._arbiter_next_probe_at.pop(group_id, None)
            self._arbiter_failure_count.pop(group_id, None)
            self._log(f"[pupu][arbiter] local silence off group={group_id}; arbiter reconnect allowed")

    async def handle(self, message: ActorInboundMessage, *, is_open_group: bool = False) -> None:
        if await self._handle_command(message):
            return
        if message.text.lstrip().startswith("/"):
            return
        if is_open_group and self.is_group_silenced(message.group_id):
            return
        sid = message.session_id
        if sid not in self._buffers:
            self._buffers[sid] = _Buffer(message=message, is_open_group=is_open_group)
        buf = self._buffers[sid]
        if message.text:
            buf.texts.append(message.text)
        buf.image_urls.extend(message.image_urls or [])
        buf.message = message
        buf.is_open_group = bool(buf.is_open_group or is_open_group)
        speaker = _speaker_item(message)
        signature = (speaker["person_key"], speaker["qq_id"], speaker["display_name"])
        if not any(
            (
                str(item.get("person_key") or ""),
                str(item.get("qq_id") or ""),
                str(item.get("display_name") or ""),
            )
            == signature
            for item in buf.speakers
        ):
            buf.speakers.append(speaker)

        try:
            if cancel_wait_timer(sid):
                self._log(f"[pupu] wait_followup timer cancelled: session={sid}")
        except Exception as exc:
            self._log(f"[pupu] wait_followup cancel failed: session={sid} error={exc}")
        if is_followup_eligible(sid):
            register_sender(sid, self._make_followup_sender(sid, message))

        if buf.is_open_group:
            await self._observe_open_group(buf, message)
            return

        if self._session_phase.get(sid) == "processing":
            return
        old = self._debounce_tasks.get(sid)
        if old:
            old.cancel()
        self._debounce_tasks[sid] = asyncio.create_task(self._debounce_flush(sid))

    def _make_followup_sender(self, sid: str, message: ActorInboundMessage):
        target = ActorOutboundTarget(
            session_id=sid,
            user_id=message.user_id,
            group_id="",
        )
        loop = asyncio.get_running_loop()

        def _send(text: str) -> None:
            async def _send_with_context() -> None:
                if self._context is None:
                    await self._send_text(target, text)
                    return
                with activate_instance_context(self._context):
                    await self._send_text(target, text)

            asyncio.run_coroutine_threadsafe(_send_with_context(), loop)

        return _send

    async def _debounce_flush(self, sid: str) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        if (self._buffers.get(sid) or _Buffer(ActorInboundMessage(sid, sid, "", "", ""))).is_open_group:
            self._buffers.pop(sid, None)
            self._debounce_tasks.pop(sid, None)
            self._session_phase.pop(sid, None)
            return
        if self._session_phase.get(sid) == "processing":
            return
        self._session_phase[sid] = "processing"
        buf = self._buffers.pop(sid, None)
        self._debounce_tasks.pop(sid, None)
        if not buf:
            self._session_phase.pop(sid, None)
            return
        try:
            await self._process_buffer(buf, persist_user=True)
        finally:
            self._session_phase.pop(sid, None)
            if sid in self._buffers and sid not in self._debounce_tasks:
                self._debounce_tasks[sid] = asyncio.create_task(self._debounce_flush(sid))

    async def _process_buffer(self, buf: _Buffer, *, persist_user: bool) -> None:
        message = buf.message
        combined_text = "\n".join(text for text in buf.texts if text)
        if not combined_text and not buf.image_urls:
            return
        if message.group_id and self.is_group_silenced(message.group_id):
            return
        _log("recv", message, combined_text or "[图片]")
        speaker_payload = json.dumps(buf.speakers or [], ensure_ascii=False)
        if not persist_user and combined_text:
            save_message_with_speaker(
                "user",
                _with_turn_timestamp(combined_text),
                message.session_id,
                source=CHAT,
                speaker_key=speaker_payload,
                speaker_name=message.speaker_name or message.user_name,
                speaker_qq=message.user_id,
            )
        reply = await asyncio.to_thread(
            chat,
            combined_text,
            message.session_id,
            message.is_admin,
            buf.image_urls or None,
            None,
            CHAT,
            context_session=message.session_id,
            identity_session=_identity_session_for_context(
                message.session_id,
                message.identity_session,
            ),
            persist_user=persist_user,
            speaker_key=speaker_payload,
            speaker_name=message.speaker_name or message.user_name,
            speaker_qq=message.user_id,
        )
        if message.group_id and self.is_group_silenced(message.group_id):
            return
        _log("send", message, reply)
        await self._send_text(
            ActorOutboundTarget(
                session_id=message.session_id,
                user_id=message.user_id,
                group_id=message.group_id,
                reply_at_user_id=message.reply_at_user_id,
            ),
            reply,
        )
        if buf.is_open_group:
            await self._post_self_reply_observe(message, reply)

    def _arbiter_observe_url(self) -> str:
        return f"{load_arbiter_base_url().rstrip('/')}/api/observe"

    def _arbiter_await_url(self) -> str:
        return f"{load_arbiter_base_url().rstrip('/')}/api/await_decision"

    def _arbiter_can_probe(self, group_id: str) -> bool:
        if self.is_group_silenced(group_id):
            return False
        if not self._arbiter_unavailable.get(group_id):
            return True
        return time.monotonic() >= float(self._arbiter_next_probe_at.get(group_id, 0.0))

    def _mark_arbiter_success(self, group_id: str) -> None:
        was_unavailable = bool(self._arbiter_unavailable.get(group_id))
        self._arbiter_failure_count[group_id] = 0
        self._arbiter_next_probe_at.pop(group_id, None)
        if was_unavailable:
            self._arbiter_unavailable[group_id] = False
            self._log(f"[pupu][arbiter] recovered group={group_id}")

    def _mark_arbiter_failure(self, group_id: str, *, where: str, exc: Exception) -> float:
        count = int(self._arbiter_failure_count.get(group_id, 0)) + 1
        self._arbiter_failure_count[group_id] = count
        probe_seconds = load_arbiter_unavailable_probe_seconds()
        if count >= 3:
            self._arbiter_next_probe_at[group_id] = time.monotonic() + probe_seconds
            if not self._arbiter_unavailable.get(group_id):
                self._arbiter_unavailable[group_id] = True
                self._log(
                    f"[pupu][arbiter] unavailable group={group_id} after={count} "
                    f"where={where} err={type(exc).__name__}: {exc}; "
                    f"retry_every={probe_seconds:.0f}s"
                )
            return probe_seconds
        self._log(
            f"[pupu][arbiter] {where} error group={group_id} "
            f"attempt={count}/3 err={type(exc).__name__}: {exc}"
        )
        return 0.0

    async def _post_observe(self, payload: dict) -> dict | None:
        group_id = str(payload.get("group_id") or "")
        if self.is_group_silenced(group_id) or not self._arbiter_can_probe(group_id):
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._arbiter_observe_url(), json=payload)
                response.raise_for_status()
                data = response.json()
            self._mark_arbiter_success(group_id)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            self._mark_arbiter_failure(group_id, where="observe", exc=exc)
            return None

    def _bot_id(self) -> str:
        configured = load_bot_id()
        if configured:
            return configured
        bot_qq = self._bot_qq()
        if bot_qq:
            return bot_qq
        if self._context is not None:
            return str(getattr(self._context, "instance_id", "") or "").strip()
        return "bot"

    def _bot_qq(self) -> str:
        try:
            return str(self._bot_qq_getter() or "").strip()
        except Exception:
            return ""

    async def _observe_open_group(self, buf: _Buffer, message: ActorInboundMessage) -> None:
        group_id = message.group_id
        if not group_id:
            return
        bot_id = self._bot_id()
        person = _canonical_speaker(message)
        payload = {
            "group_id": group_id,
            "message_id": message.message_id or f"local:{time.time_ns()}",
            "speaker_qq": message.user_id,
            "speaker_name": str(person.get("display_name") or message.user_name or ""),
            "speaker_person_key": str(person.get("person_key") or ""),
            "speaker_is_bot": bool(message.speaker_is_bot),
            "text": _strip_prefix(message.text),
            "ts": "",
            "reporter": {
                "bot_id": bot_id,
                "qq": self._bot_qq(),
                "name": bot_id,
                "persona_brief": bot_id,
                "min_bot_gap_seconds": 10,
                "max_consecutive_bot_turns": load_max_consecutive_bot_turns(),
            },
            "peers": [],
        }
        already = group_id in self._arbiter_subscribers and not self._arbiter_subscribers[group_id].done()
        response = await self._post_observe(payload)
        if not already:
            initial_since = int((response or {}).get("latest_decision_id") or 0)
            self._ensure_subscriber(group_id, message.session_id, initial_since)

    def _ensure_subscriber(self, group_id: str, sid: str, initial_since: int | None) -> None:
        if not group_id or self.is_group_silenced(group_id):
            return
        existing = self._arbiter_subscribers.get(group_id)
        if existing and not existing.done():
            return
        if initial_since is not None:
            self._arbiter_last_decision_id.setdefault(group_id, int(initial_since))
        self._arbiter_subscribers[group_id] = asyncio.create_task(
            self._arbiter_decision_subscriber(group_id, sid)
        )

    async def _arbiter_decision_subscriber(self, group_id: str, sid: str) -> None:
        bot_id = self._bot_id()
        timeout_sec = load_arbiter_subscribe_timeout_seconds()
        backoff = 1.0
        while True:
            if self.is_group_silenced(group_id):
                return
            try:
                since = int(self._arbiter_last_decision_id.get(group_id, 0))
                params = {
                    "group_id": group_id,
                    "since": str(since),
                    "timeout": str(timeout_sec),
                }
                async with httpx.AsyncClient(timeout=timeout_sec + 10.0) as client:
                    response = await client.get(self._arbiter_await_url(), params=params)
                    response.raise_for_status()
                    body = response.json()
                self._mark_arbiter_success(group_id)
                decision = (body or {}).get("decision")
                if not decision:
                    backoff = 1.0
                    continue
                decision_id = int(decision.get("decision_id") or 0)
                speaker = str(decision.get("speaker") or "none")
                reason = str(decision.get("reason") or "")
                confidence = float(decision.get("confidence") or 0.0)
                self._log(
                    "[pupu][arbiter] decision "
                    f"group={group_id} decision_id={decision_id} me={bot_id} "
                    f"speaker={speaker} reason={reason} conf={confidence:.2f}"
                )
                self._arbiter_last_decision_id[group_id] = decision_id
                backoff = 1.0
                if speaker != bot_id:
                    continue
                if self._session_phase.get(sid) == "processing":
                    continue
                buf = self._buffers.get(sid)
                if not buf:
                    continue
                asyncio.create_task(self.act_as_selected_speaker(sid))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                probe_sleep = self._mark_arbiter_failure(group_id, where="subscriber", exc=exc)
                try:
                    await asyncio.sleep(probe_sleep if probe_sleep > 0 else min(backoff, 15.0))
                except asyncio.CancelledError:
                    return
                backoff = 1.0 if probe_sleep > 0 else min(backoff * 2.0, 15.0)

    async def act_as_selected_speaker(self, sid: str) -> None:
        buf = self._buffers.get(sid)
        if not buf:
            return
        if self._session_phase.get(sid) == "processing":
            return
        self._session_phase[sid] = "processing"
        buf = self._buffers.pop(sid, None)
        if not buf:
            self._session_phase.pop(sid, None)
            return
        try:
            await self._process_buffer(buf, persist_user=False)
        except Exception as exc:
            self._log(f"[pupu] act_as_selected_speaker error ({sid}): {exc}")
            try:
                await self._send_text(
                    ActorOutboundTarget(
                        session_id=buf.message.session_id,
                        user_id=buf.message.user_id,
                        group_id=buf.message.group_id,
                    ),
                    "呃，脑子卡了一下",
                )
            except Exception:
                pass
        finally:
            self._session_phase.pop(sid, None)

    async def _post_self_reply_observe(self, message: ActorInboundMessage, text: str) -> None:
        if not message.group_id or not text:
            return
        if self.is_group_silenced(message.group_id):
            return
        bot_id = self._bot_id()
        payload = {
            "group_id": message.group_id,
            "message_id": f"self:{bot_id}:{time.time_ns()}",
            "speaker_qq": "",
            "speaker_name": bot_id,
            "speaker_person_key": "instance",
            "speaker_is_bot": True,
            "text": text,
            "ts": "",
            "reporter": {
                "bot_id": bot_id,
                "qq": "",
                "name": bot_id,
                "persona_brief": bot_id,
                "min_bot_gap_seconds": 10,
                "max_consecutive_bot_turns": load_max_consecutive_bot_turns(),
            },
        }
        await self._post_observe(payload)


def prefixed_open_group_text(message: ActorInboundMessage) -> str:
    if not message.text:
        return _speaker_prefix(message).strip()
    return _speaker_prefix(message) + message.text
