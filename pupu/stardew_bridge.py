"""Local HTTP bridge used by the Stardew Valley NPC integration."""

from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .agent import chat
from .llm import preflight_model_providers
from .logging_utils import setup_runtime_logging
from .memory import init_db
from .message_sources import CHAT
from .sessions import OWNER_SESSION

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18787
DEFAULT_REPLY_HINT = (
    "这是星露谷里用户走到仆仆 NPC 身边发起的对话。回复要短一点，像游戏内 NPC "
    "对话框里说的话；可以自然结合地点、季节、时间、农场状态，但不要把上下文标签念出来。"
)

ChatFunc = Callable[..., str]


@dataclass(slots=True)
class StardewBridgeConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    session_id: str = OWNER_SESSION
    token: str = ""
    reply_hint: str = DEFAULT_REPLY_HINT


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def load_stardew_bridge_config() -> StardewBridgeConfig:
    return StardewBridgeConfig(
        host=os.environ.get("PUPU_STARDEW_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
        port=_env_int("PUPU_STARDEW_PORT", DEFAULT_PORT),
        session_id=os.environ.get("PUPU_STARDEW_SESSION_ID", OWNER_SESSION).strip()
        or OWNER_SESSION,
        token=os.environ.get("PUPU_STARDEW_TOKEN", "").strip(),
        reply_hint=os.environ.get("PUPU_STARDEW_REPLY_HINT", DEFAULT_REPLY_HINT).strip()
        or DEFAULT_REPLY_HINT,
    )


def _compact_text(value: object, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _context_lines(context: dict[str, Any]) -> list[str]:
    labels = {
        "player": "玩家",
        "farm": "农场",
        "location": "地点",
        "season": "季节",
        "day": "日期",
        "year": "年份",
        "time": "时间",
        "weather": "天气",
        "money": "金钱",
        "npc_name": "对话对象",
    }
    lines = []
    for key, label in labels.items():
        value = context.get(key)
        if value not in (None, ""):
            lines.append(f"{label}={_compact_text(value, 60)}")
    return lines


def format_stardew_user_input(text: str, context: dict[str, Any] | None = None) -> str:
    text = str(text or "").strip()
    lines = _context_lines(context or {})
    if not lines:
        return f"[星露谷NPC] {text}"
    return f"[星露谷NPC | {'; '.join(lines)}] {text}"


def _is_authorized(headers, token: str) -> bool:
    if not token:
        return True
    auth = headers.get("Authorization", "").strip()
    if auth == f"Bearer {token}":
        return True
    return headers.get("X-PuPu-Token", "").strip() == token


def handle_chat_payload(
    payload: dict[str, Any],
    *,
    config: StardewBridgeConfig | None = None,
    chat_func: ChatFunc | None = None,
) -> dict[str, Any]:
    config = config or load_stardew_bridge_config()
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")

    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")

    session_id = str(payload.get("session_id") or config.session_id or OWNER_SESSION).strip()
    if not session_id:
        session_id = OWNER_SESSION

    raw_context = payload.get("context")
    context = raw_context if isinstance(raw_context, dict) else {}
    user_input = format_stardew_user_input(text, context)

    runner = chat_func or chat
    print(
        "[pupu][stardew-npc] recv "
        f"session={session_id} text={_compact_text(text, 120)}"
    )
    reply = runner(
        user_input,
        session_id,
        is_admin=False,
        image_urls=[],
        reply_speed_hint=config.reply_hint,
        message_source=CHAT,
    )
    print(
        "[pupu][stardew-npc] send "
        f"session={session_id} chars={len(reply or '')}"
    )
    return {
        "ok": True,
        "session_id": session_id,
        "reply": reply,
    }


class _StardewBridgeHandler(BaseHTTPRequestHandler):
    server_version = "PuPuStardewNpcBridge/0.1"

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send_json(200, {"ok": True, "service": "pupu-stardew-npc-bridge"})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        config: StardewBridgeConfig = self.server.pupu_config  # type: ignore[attr-defined]
        if not _is_authorized(self.headers, config.token):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if urlparse(self.path).path != "/chat":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            payload = self._read_json()
            self._send_json(200, handle_chat_payload(payload, config=config))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            print("[pupu][stardew-npc] failed\n" + traceback.format_exc())
            self._send_json(500, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib API name
        print("[pupu][stardew-npc][http] " + (format % args))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        if length > 65536:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(config: StardewBridgeConfig | None = None) -> ThreadingHTTPServer:
    config = config or load_stardew_bridge_config()
    server = ThreadingHTTPServer((config.host, config.port), _StardewBridgeHandler)
    server.pupu_config = config  # type: ignore[attr-defined]
    return server


def main() -> None:
    setup_runtime_logging()
    init_db()
    preflight_model_providers()
    config = load_stardew_bridge_config()
    server = create_server(config)
    print(
        "[pupu][stardew-npc] bridge started "
        f"http://{config.host}:{config.port} session={config.session_id}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[pupu][stardew-npc] bridge stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
