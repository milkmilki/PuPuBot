"""Lightweight OneBot v11 reverse WebSocket transport for actor mode."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect


InboundHandler = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class OneBotConnectionInfo:
    self_id: str = ""
    connected: bool = False


class OneBotTransport:
    """Serve one NapCat reverse WebSocket endpoint for a single actor."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        on_event: InboundHandler,
        log: Callable[[str], None] | None = None,
        access_token: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self._on_event = on_event
        self._log = log or (lambda text: None)
        self._access_token = str(access_token or "").strip()
        self._app = FastAPI(title=f"PuPu OneBot actor {self.port}")
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._ws: WebSocket | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self.info = OneBotConnectionInfo()
        self._configure_routes()

    def _configure_routes(self) -> None:
        @self._app.websocket("/onebot/v11/ws")
        async def onebot_ws(websocket: WebSocket) -> None:
            if self._access_token:
                token = websocket.headers.get("authorization", "")
                token = token.removeprefix("Bearer ").strip()
                query_token = str(websocket.query_params.get("access_token") or "").strip()
                if token != self._access_token and query_token != self._access_token:
                    await websocket.close(code=1008)
                    return
            await websocket.accept()
            await self._handle_socket(websocket)

    async def start(self) -> None:
        if self._server_task is not None and not self._server_task.done():
            return
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        self._log(
            "[PuPu Actor] NapCat reverse WebSocket listening at "
            f"ws://127.0.0.1:{self.port}/onebot/v11/ws"
        )

    async def stop(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError("OneBot transport stopped"))
        self._pending.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._server_task = None
        self.info.connected = False

    async def _handle_socket(self, websocket: WebSocket) -> None:
        old = self._ws
        if old is not None and old is not websocket:
            try:
                await old.close()
            except Exception:
                pass
        self._ws = websocket
        self.info.connected = True
        self.info.self_id = str(
            websocket.headers.get("x-self-id")
            or websocket.query_params.get("self_id")
            or ""
        ).strip()
        self._log(
            "[pupu][actor] NapCat connected"
            + (f" self_id={self.info.self_id}" if self.info.self_id else "")
        )
        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                echo = str(data.get("echo") or "")
                if echo and echo in self._pending:
                    future = self._pending.pop(echo)
                    if not future.done():
                        future.set_result(data)
                    continue
                await self._on_event(data)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"[pupu][actor] OneBot socket error: {type(exc).__name__}: {exc}")
        finally:
            if self._ws is websocket:
                self._ws = None
            self.info.connected = False
            self._log("[pupu][actor] NapCat disconnected")
            for echo, future in list(self._pending.items()):
                self._pending.pop(echo, None)
                if not future.done():
                    future.set_exception(RuntimeError("NapCat disconnected"))

    async def call_action(
        self,
        action: str,
        params: dict,
        *,
        timeout: float = 30.0,
    ) -> dict:
        if self._ws is None:
            raise RuntimeError("NapCat is not connected")
        echo = f"pupu-{secrets.token_hex(8)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        self._pending[echo] = future
        payload = {"action": action, "params": params, "echo": echo}
        try:
            async with self._write_lock:
                await self._ws.send_json(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self._pending.pop(echo, None)
            raise

    async def send_private_text(self, user_id: str | int, text: str) -> None:
        await self.call_action(
            "send_private_msg",
            {"user_id": int(user_id), "message": str(text or "")},
        )

    async def send_group_text(self, group_id: str | int, text: str) -> None:
        await self.call_action(
            "send_group_msg",
            {"group_id": int(group_id), "message": str(text or "")},
        )

    async def get_login_info(self) -> dict:
        try:
            response = await self.call_action("get_login_info", {}, timeout=10.0)
        except Exception:
            return {}
        data = response.get("data")
        return data if isinstance(data, dict) else {}


def parse_onebot_message_segments(message) -> tuple[str, list[str], list[str]]:
    """Return text, image URLs, at-target QQs from OneBot v11 message payload."""
    text_parts: list[str] = []
    image_urls: list[str] = []
    at_targets: list[str] = []
    if isinstance(message, str):
        return message.strip(), [], []
    if not isinstance(message, list):
        return "", [], []
    for item in message:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type") or "")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if seg_type == "text":
            text_parts.append(str(data.get("text") or ""))
        elif seg_type == "face":
            face_id = str(data.get("id") or "")
            text_parts.append(f"[表情{face_id}]" if face_id else "[表情]")
        elif seg_type == "at":
            qq = str(data.get("qq") or "").strip()
            if qq:
                at_targets.append(qq)
                text_parts.append("@全体成员" if qq == "all" else f"@{qq}")
        elif seg_type in {"image", "mface"}:
            if seg_type == "mface":
                continue
            subtype = data.get("subType", data.get("sub_type"))
            try:
                if subtype is not None and int(subtype) != 0:
                    continue
            except Exception:
                pass
            summary = str(data.get("summary") or "")
            if "表情" in summary:
                continue
            url = str(data.get("url") or data.get("file") or "").strip()
            if url:
                image_urls.append(url)
    return "".join(text_parts).strip(), image_urls, at_targets
