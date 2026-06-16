"""Minimal stdio MCP server exposing PuPu's local tool registry to Codex CLI."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from typing import Any

from .tools import execute_tool, is_admin_tool
from .tooling import get_registry


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _image_urls() -> list[str]:
    raw = os.environ.get("PUPU_MCP_IMAGE_URLS", "[]")
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _tool_definitions() -> list[dict[str, Any]]:
    exposure = os.environ.get("PUPU_MCP_EXPOSURE", "chat")
    is_admin = _env_bool("PUPU_MCP_IS_ADMIN", False)
    tools = []
    for definition in get_registry().get_api_definitions(exposure):
        name = str(definition.get("name", ""))
        if is_admin_tool(name) and not is_admin:
            continue
        tools.append(
            {
                "name": name,
                "description": definition.get("description", ""),
                "inputSchema": definition.get("input_schema", {"type": "object"}),
            }
        )
    return tools


def _result_to_text(result) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", ""))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}

    is_admin = _env_bool("PUPU_MCP_IS_ADMIN", False)
    session_id = os.environ.get("PUPU_MCP_SESSION_ID", "default")
    if is_admin_tool(name) and not is_admin:
        text = "权限不足：只有管理员才能使用文件和命令工具。"
    else:
        # Tool helpers log with print(); redirect that away from MCP stdout.
        with contextlib.redirect_stdout(sys.stderr):
            text = _result_to_text(
                execute_tool(
                    name,
                    arguments,
                    image_urls=_image_urls(),
                    session_id=session_id,
                    reason_hint="Codex MCP tool call",
                )
            )

    return {"content": [{"type": "text", "text": text}]}


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = str(message.get("method", ""))
    params = message.get("params") or {}
    if request_id is None:
        return None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pupu", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": _tool_definitions()}
        elif method == "tools/call":
            result = _handle_tool_call(params if isinstance(params, dict) else {})
        elif method == "ping":
            result = {}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        print(traceback.format_exc(), file=sys.stderr)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(exc)},
        }


def _write_response(response: dict[str, Any], *, framed: bool) -> None:
    payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if framed:
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    else:
        print(payload.decode("utf-8"), flush=True)


def _try_read_message(buffer: bytes) -> tuple[bytes | None, bool, bytes]:
    stripped = buffer.lstrip()
    leading = len(buffer) - len(stripped)
    if leading:
        buffer = stripped

    if buffer.lower().startswith(b"content-length:"):
        header_end = buffer.find(b"\r\n\r\n")
        separator_len = 4
        if header_end == -1:
            header_end = buffer.find(b"\n\n")
            separator_len = 2
        if header_end == -1:
            return None, True, buffer

        header = buffer[:header_end].decode("ascii", errors="replace")
        length = None
        for line in header.splitlines():
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except Exception:
                    length = None
                break
        if length is None:
            return None, True, b""

        body_start = header_end + separator_len
        body_end = body_start + length
        if len(buffer) < body_end:
            return None, True, buffer
        return buffer[body_start:body_end], True, buffer[body_end:]

    newline = buffer.find(b"\n")
    if newline == -1:
        return None, False, buffer
    line = buffer[:newline].strip()
    return line, False, buffer[newline + 1 :]


def main() -> None:
    buffer = b""
    while True:
        chunk = sys.stdin.buffer.read1(4096)
        if not chunk:
            break
        buffer += chunk
        while buffer:
            payload, framed, buffer = _try_read_message(buffer)
            if payload is None:
                break
            if not payload:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            response = _handle_request(message)
            if response is not None:
                _write_response(response, framed=framed)


if __name__ == "__main__":
    main()
