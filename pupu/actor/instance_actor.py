"""Per-instance actor runtime used by console actor mode and CLI."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

from pupu.app_config import apply_app_config_env, default_napcat_settings
from pupu.backup import maybe_run_daily_backup
from pupu.command_service import CommandContext, execute_command
from pupu.config import (
    is_private_reply_allowed,
    load_first_numeric_owner_id,
    load_open_group_ids,
    load_owner_id_set,
    load_peer_config,
)
from pupu.dialogue_loop import register_sender
from pupu.hooks import emit_instance_status
from pupu.instance_context import InstanceContext, activate_instance_context
from pupu.llm import ProviderConfigError, preflight_model_providers
from pupu.logging_utils import close_current_instance_log_sinks, setup_runtime_logging
from pupu.maintenance import maybe_run_daily_memu_tidy
from pupu.memory import init_db
from pupu.proactive import proactive_loop
from pupu.proactive_control import is_proactive_enabled
from pupu.scheduler import sender_scheduled_tasks_loop
from pupu.sessions import OWNER_SESSION
from pupu.storage.people import OWNER_PERSON_KEY, qq_person_key

from .message_buffer import MessageBuffer, prefixed_open_group_text
from .onebot_transport import OneBotTransport, parse_onebot_message_segments
from .types import ActorInboundMessage, ActorOutboundTarget

MAINTENANCE_LOOP_INTERVAL_SECONDS = 30 * 60


def _split_message(text: str) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return [value]
    parts = [line.strip() for line in value.splitlines() if line.strip()]
    return parts if parts else [value]


async def _sleep_before_next_segment(next_segment: str) -> None:
    import random

    typing_time = min(4, max(0.8, len(next_segment) * random.uniform(0.05, 0.15)))
    await asyncio.sleep(typing_time)


class InstanceActor:
    def __init__(
        self,
        context: InstanceContext,
        *,
        emit_log: Callable[[str], None] | None = None,
        cli_send: Callable[[str], None] | None = None,
        preflight: bool = True,
        start_background_tasks: bool = True,
    ) -> None:
        self.context = context
        self._emit_log = emit_log or (lambda text: None)
        self._cli_send = cli_send
        self._preflight = bool(preflight)
        self._start_background_tasks = bool(start_background_tasks)
        self._transport: OneBotTransport | None = None
        self._tasks: set[asyncio.Task] = set()
        self._started = False
        self._stopping = False
        self.buffer = MessageBuffer(
            send_text=self.send_text,
            handle_command=self._handle_inbound_command,
            log=self._log,
            bot_qq_getter=self._current_bot_qq,
            context=self.context,
        )

    @classmethod
    def from_instance_dir(
        cls,
        instance_dir: str | Path,
        *,
        emit_log: Callable[[str], None] | None = None,
        cli_send: Callable[[str], None] | None = None,
        preflight: bool = True,
        start_background_tasks: bool = True,
    ) -> "InstanceActor":
        return cls(
            InstanceContext.from_instance_dir(instance_dir),
            emit_log=emit_log,
            cli_send=cli_send,
            preflight=preflight,
            start_background_tasks=start_background_tasks,
        )

    @property
    def running(self) -> bool:
        return self._started and not self._stopping

    @property
    def transport(self) -> OneBotTransport | None:
        return self._transport

    def _current_bot_qq(self) -> str:
        if self._transport is None:
            return ""
        return str(self._transport.info.self_id or "").strip()

    def _log(self, text: str) -> None:
        print(text)
        line = text if text.endswith("\n") else text + "\n"
        self._emit_log(line)

    def _create_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        if self._started:
            return
        with activate_instance_context(self.context):
            await emit_instance_status("starting")
            try:
                apply_app_config_env()
                self.context.data_dir.mkdir(parents=True, exist_ok=True)
                self.context.logs_dir.mkdir(parents=True, exist_ok=True)
                setup_runtime_logging()
                init_db()
                if self._preflight:
                    try:
                        preflight_model_providers(require_chat=True)
                    except ProviderConfigError:
                        raise
                if self.context.qq_mode == "napcat":
                    settings = self._read_napcat_settings()
                    self._transport = OneBotTransport(
                        host=str(settings["host"]),
                        port=int(settings["port"]),
                        access_token=str(settings.get("access_token") or ""),
                        expected_self_id=str(settings.get("expected_self_id") or ""),
                        on_event=self._handle_onebot_event_with_context,
                        log=self._log,
                    )
                    await self._transport.start()
                    self._log("[PuPu QQ] mode: NapCat actor (OneBot v11)")
                elif self.context.qq_mode == "cli":
                    self._log("[PuPu CLI] mode: actor")
                else:
                    raise RuntimeError(
                        f"actor runtime only supports napcat/cli for now, got {self.context.qq_mode!r}"
                    )
                if self._start_background_tasks:
                    self._create_task(self._run_callable_with_context(self._scheduler_loop))
                    self._create_task(self._run_callable_with_context(self._maintenance_loop))
                    if is_proactive_enabled():
                        self._start_proactive_loop()
                self._started = True
                await emit_instance_status("running")
            except Exception as exc:
                await emit_instance_status(
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                for task in list(self._tasks):
                    task.cancel()
                if self._tasks:
                    await asyncio.gather(*self._tasks, return_exceptions=True)
                self._tasks.clear()
                if self._transport is not None:
                    await self._transport.stop()
                    self._transport = None
                self._started = False
                close_current_instance_log_sinks()
                raise

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        with activate_instance_context(self.context):
            await emit_instance_status("stopping")
            try:
                await self.buffer.stop()
                for task in list(self._tasks):
                    task.cancel()
                if self._tasks:
                    await asyncio.gather(*self._tasks, return_exceptions=True)
                self._tasks.clear()
                if self._transport is not None:
                    await self._transport.stop()
                    self._transport = None
                self._started = False
                await emit_instance_status("stopped")
                close_current_instance_log_sinks()
            finally:
                self._stopping = False

    async def _run_with_context(self, coro) -> None:
        with activate_instance_context(self.context):
            await coro

    async def _run_callable_with_context(self, func) -> None:
        with activate_instance_context(self.context):
            await func()

    def _read_napcat_settings(self) -> dict:
        settings = default_napcat_settings()
        cfg = self._read_instance_config()
        if cfg.get("port"):
            try:
                settings["port"] = int(cfg["port"])
            except Exception:
                pass
        bot_id = str(cfg.get("bot_id") or "").strip()
        settings["expected_self_id"] = bot_id if bot_id.isdigit() else ""
        return settings

    def _read_instance_config(self) -> dict:
        try:
            data = json.loads(self.context.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _scheduler_loop(self) -> None:
        await sender_scheduled_tasks_loop(self._send_by_session)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                backup_report = await asyncio.to_thread(maybe_run_daily_backup)
                if backup_report:
                    self._log(f"[pupu] auto backup\n{backup_report}")
                memu_report = await asyncio.to_thread(maybe_run_daily_memu_tidy)
                if memu_report:
                    self._log(f"[pupu] auto memu tidy\n{memu_report}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"[pupu] maintenance loop failed: {exc}")
            await asyncio.sleep(MAINTENANCE_LOOP_INTERVAL_SECONDS)

    def _start_proactive_loop(self) -> str:
        if any(getattr(task, "_pupu_proactive", False) for task in self._tasks if not task.done()):
            return "主动消息已开启，后台循环正在运行。"
        loop = asyncio.get_running_loop()
        if self.context.qq_mode != "cli":
            with activate_instance_context(self.context):
                owner_qq = load_first_numeric_owner_id()
        else:
            owner_qq = None
        if self.context.qq_mode != "cli" and owner_qq is None:
            return "主动消息已开启，但没有配置数字 owner QQ，后台循环暂时无法投递。"

        def send_owner_followup(text: str) -> None:
            async def _send_with_context() -> None:
                with activate_instance_context(self.context):
                    await self._send_by_session(OWNER_SESSION, text)

            asyncio.run_coroutine_threadsafe(_send_with_context(), loop)

        with activate_instance_context(self.context):
            register_sender(OWNER_SESSION, send_owner_followup)

        async def send_to_owner(text: str) -> None:
            await self._send_by_session(OWNER_SESSION, text)

        task = self._create_task(self._run_with_context(proactive_loop(send_to_owner)))
        setattr(task, "_pupu_proactive", True)
        return "主动消息已开启，后台循环已启动。"

    def _stop_proactive_loop(self) -> str:
        for task in list(self._tasks):
            if getattr(task, "_pupu_proactive", False):
                task.cancel()
        return "主动消息已关闭。"

    async def _send_by_session(self, session_id: str, text: str) -> None:
        sid = str(session_id or "")
        if self.context.qq_mode == "cli":
            await self.send_text(ActorOutboundTarget(session_id=sid), text)
            return
        if sid == OWNER_SESSION:
            owner_qq = load_first_numeric_owner_id()
            if owner_qq is None:
                self._log("[pupu] scheduled: no numeric owner QQ configured")
                return
            await self.send_text(
                ActorOutboundTarget(session_id=sid, user_id=str(owner_qq)),
                text,
            )
            return
        if sid.startswith("private_"):
            tail = sid[8:]
            if tail.isdigit():
                await self.send_text(ActorOutboundTarget(session_id=sid, user_id=tail), text)
            return
        if sid.startswith("group_"):
            tail = sid[6:]
            if tail.isdigit():
                await self.send_text(ActorOutboundTarget(session_id=sid, group_id=tail), text)

    async def send_text(self, target: ActorOutboundTarget, text: str) -> None:
        if self.context.qq_mode == "cli":
            line = str(text).rstrip()
            if self._cli_send is not None:
                self._cli_send(line)
            else:
                self._emit_log(line + "\n")
            return
        if self._transport is None:
            raise RuntimeError("OneBot transport is not running")
        segments = _split_message(text)
        for index, segment in enumerate(segments):
            outgoing = segment
            if target.group_id:
                if index == 0 and target.reply_at_user_id:
                    outgoing = f"[CQ:at,qq={target.reply_at_user_id}] {segment}"
                await self._transport.send_group_text(target.group_id, outgoing)
            elif target.user_id:
                await self._transport.send_private_text(target.user_id, outgoing)
            else:
                raise RuntimeError(f"cannot route outbound text for session {target.session_id}")
            if index < len(segments) - 1:
                await _sleep_before_next_segment(segments[index + 1])

    async def _handle_onebot_event_with_context(self, event: dict) -> None:
        await self.handle_onebot_event(event)

    async def handle_onebot_event(self, event: dict) -> None:
        with activate_instance_context(self.context):
            await self._handle_onebot_event(event)

    async def _handle_onebot_event(self, event: dict) -> None:
        if str(event.get("post_type") or "") != "message":
            return
        message_type = str(event.get("message_type") or "")
        text, image_urls, at_targets = parse_onebot_message_segments(event.get("message"))
        if not text and not image_urls:
            return
        user_id = str(event.get("user_id") or "").strip()
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        nickname = str(sender.get("nickname") or sender.get("card") or user_id).strip() or user_id
        if message_type == "private":
            if not is_private_reply_allowed(user_id):
                self._log(f"[pupu] private message ignored by whitelist: user_id={user_id}")
                return
            message = self._private_message(user_id, nickname, text, image_urls, event)
            await self.buffer.handle(message)
            return
        if message_type != "group":
            return
        group_id = str(event.get("group_id") or "").strip()
        if not group_id:
            return
        is_open_group = group_id in load_open_group_ids()
        self_id = str(event.get("self_id") or (self._transport.info.self_id if self._transport else "") or "")
        is_at_me = bool(self_id and self_id in at_targets)
        if is_open_group:
            raw = self._group_message(user_id, nickname, group_id, text, image_urls, event)
            if text.lstrip().startswith("/"):
                await self.buffer.handle(raw, is_open_group=True)
                return
            message = ActorInboundMessage(
                session_id=raw.session_id,
                identity_session=raw.identity_session,
                user_id=raw.user_id,
                user_name=raw.user_name,
                text=prefixed_open_group_text(raw),
                image_urls=raw.image_urls,
                is_admin=raw.is_admin,
                speaker_key=raw.speaker_key,
                speaker_name=raw.speaker_name,
                speaker_is_bot=raw.speaker_is_bot,
                group_id=raw.group_id,
                message_id=raw.message_id,
                surface=raw.surface,
            )
            await self.buffer.handle(message, is_open_group=True)
            return
        if not is_at_me:
            return
        cleaned = text
        if self_id:
            cleaned = cleaned.replace(f"@{self_id}", "").strip()
        message = self._group_message(user_id, nickname, group_id, cleaned, image_urls, event)
        message = replace(message, reply_at_user_id=user_id)
        await self.buffer.handle(message)

    def _identity_session_for_user(self, user_id: str) -> str:
        return OWNER_SESSION if user_id in load_owner_id_set() else f"private_{user_id}"

    def _person_key_for_user(self, user_id: str) -> str:
        return OWNER_PERSON_KEY if user_id in load_owner_id_set() else qq_person_key(user_id)

    def _is_admin(self, user_id: str) -> bool:
        return user_id in load_owner_id_set()

    def _peer_info(self, user_id: str) -> tuple[str, bool]:
        peer = load_peer_config()
        peer_qq = str(peer.get("qq") or "").strip()
        peer_name = str(peer.get("name") or "").strip()
        if peer_qq and user_id == peer_qq:
            return peer_name or user_id, True
        return "", False

    def _private_message(
        self,
        user_id: str,
        nickname: str,
        text: str,
        image_urls: list[str],
        event: dict,
    ) -> ActorInboundMessage:
        sid = self._identity_session_for_user(user_id)
        return ActorInboundMessage(
            session_id=sid,
            identity_session=sid,
            user_id=user_id,
            user_name=nickname,
            text=text,
            image_urls=image_urls,
            is_admin=self._is_admin(user_id),
            speaker_key=self._person_key_for_user(user_id),
            speaker_name=nickname,
            group_id="",
            message_id=str(event.get("message_id") or ""),
        )

    def _group_message(
        self,
        user_id: str,
        nickname: str,
        group_id: str,
        text: str,
        image_urls: list[str],
        event: dict,
    ) -> ActorInboundMessage:
        peer_name, speaker_is_bot = self._peer_info(user_id)
        display_name = peer_name or nickname or user_id
        return ActorInboundMessage(
            session_id=f"group_{group_id}",
            identity_session=self._identity_session_for_user(user_id),
            user_id=user_id,
            user_name=display_name,
            text=text,
            image_urls=image_urls,
            is_admin=self._is_admin(user_id),
            speaker_key=self._person_key_for_user(user_id),
            speaker_name=display_name,
            speaker_is_bot=speaker_is_bot,
            group_id=group_id,
            message_id=str(event.get("message_id") or ""),
        )

    async def _handle_inbound_command(self, message: ActorInboundMessage) -> bool:
        result = await execute_command(
            message.text,
            CommandContext(
                surface=message.surface,
                context_session=message.session_id,
                identity_session=message.identity_session,
                is_admin=message.is_admin,
                user_id=message.user_id,
                group_id=message.group_id,
                can_exit=False,
            ),
            silence_getter=self.buffer.is_group_silenced,
            silence_setter=self.buffer.set_group_silence,
            proactive_starter=self._start_proactive_loop,
            proactive_stopper=self._stop_proactive_loop,
        )
        if not result.handled:
            return False
        if result.text:
            await self.send_text(
                ActorOutboundTarget(
                    session_id=message.session_id,
                    user_id=message.user_id,
                    group_id=message.group_id,
                    reply_at_user_id=message.reply_at_user_id,
                ),
                result.text,
            )
        return True

    async def handle_cli_text(self, text: str, send: Callable[[str], None]) -> bool:
        """Handle one CLI line. Returns True when CLI should exit."""
        with activate_instance_context(self.context):
            raw = str(text or "").strip()
            if not raw:
                return False
            result = await execute_command(
                raw,
                CommandContext(
                    surface="cli",
                    context_session=OWNER_SESSION,
                    identity_session=OWNER_SESSION,
                    is_admin=True,
                    user_id="owner",
                    can_exit=True,
                ),
                proactive_starter=self._start_proactive_loop,
                proactive_stopper=self._stop_proactive_loop,
            )
            if result.handled:
                if result.text:
                    send(result.text)
                return result.should_exit
            if raw.lstrip().startswith("/"):
                return False
            reply = await asyncio.to_thread(
                __import__("pupu.agent", fromlist=["chat"]).chat,
                raw,
                OWNER_SESSION,
                True,
            )
            send(reply)
            return False
