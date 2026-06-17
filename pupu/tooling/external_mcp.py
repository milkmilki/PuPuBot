"""External stdio MCP client integration for PuPu tools.

This module implements a small persistent MCP client for stdio servers. It is
not a full MCP SDK, but it follows the standard client lifecycle for tool use:
initialize, notifications/initialized, tools/list, and tools/call.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import threading
from itertools import count
from typing import Any

from .base import BaseToolServer, ToolContext, ToolSpec, ToolResult

DEFAULT_MCP_TIMEOUT_SECONDS = 25.0


def external_mcp_server_configs_from_env() -> list[dict[str, Any]]:
    """Load normalized external MCP server configs from the process env."""

    raw = (
        os.environ.get("PUPU_MCP_SERVERS_JSON", "").strip()
        or os.environ.get("PUPU_CODEX_MCP_SERVERS_JSON", "").strip()
    )
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def shutdown_external_mcp_sessions() -> None:
    """Close every persistent external MCP session."""

    for session in list(_ACTIVE_SESSIONS):
        try:
            session.close()
        except Exception:
            pass


def _safe_name(value: str, default: str = "server") -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
    safe = safe.strip("_-")
    return safe or default


def _mcp_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            parts.append(str(block.get("text") or ""))
        elif block_type:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part.strip())


def _json_rpc_error(message: dict[str, Any]) -> RuntimeError:
    return RuntimeError(f"MCP error: {message.get('error')}")


class PersistentMcpStdioSession:
    """Thread-safe persistent stdio MCP session."""

    def __init__(
        self,
        *,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        cwd: str | None,
        timeout: float,
    ) -> None:
        self.name = name
        self.command = command
        self.args = args
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._next_id = count(1)
        self._pending: dict[int, "queue.Queue[dict[str, Any] | BaseException]"] = {}
        self._stderr_tail: list[str] = []
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._closed = False
        _ACTIVE_SESSIONS.add(self)

    @property
    def stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail[-8:])

    def ensure_started(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"MCP session {self.name!r} is closed")
            if self._proc is not None and self._proc.poll() is None:
                return
            self._start_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_locked()
            _ACTIVE_SESSIONS.discard(self)

    def restart(self) -> None:
        with self._lock:
            self._stop_locked()
            if self._closed:
                raise RuntimeError(f"MCP session {self.name!r} is closed")
            self._start_locked()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.ensure_started()
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ensure_started()
        request_id = next(self._next_id)
        response_queue: "queue.Queue[dict[str, Any] | BaseException]" = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = response_queue
            try:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            except Exception:
                self._pending.pop(request_id, None)
                raise

        try:
            message = response_queue.get(timeout=self.timeout)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            raise TimeoutError(
                f"MCP request timed out: {method}; stderr={self.stderr_tail}"
            ) from exc
        if isinstance(message, BaseException):
            raise RuntimeError(
                f"MCP server stopped while waiting for {method}; stderr={self.stderr_tail}"
            ) from message
        if "error" in message:
            raise _json_rpc_error(message)
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _start_locked(self) -> None:
        child_env = os.environ.copy()
        child_env.update(self.env)
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd or None,
            env=child_env,
        )
        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            name=f"pupu-mcp-stdout-{self.name}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr_loop,
            name=f"pupu-mcp-stderr-{self.name}",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        self._initialize_locked()

    def _initialize_locked(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pupu", "version": "0.1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        for request_id, pending in list(self._pending.items()):
            pending.put(RuntimeError("MCP session closed"))
            self._pending.pop(request_id, None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except Exception:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("MCP process is not running")
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(payload)
        proc.stdin.flush()

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if isinstance(request_id, int):
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.put(message)
            return
        method = str(message.get("method") or "")
        if method:
            print(f"[pupu][mcp] notification server={self.name} method={method}")

    def _fail_pending(self, exc: BaseException) -> None:
        for request_id, pending in list(self._pending.items()):
            pending.put(exc)
            self._pending.pop(request_id, None)

    def _read_stdout_loop(self) -> None:
        try:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            while True:
                message = self._read_message(proc.stdout)
                if message is None:
                    self._fail_pending(EOFError("MCP stdout closed"))
                    break
                self._dispatch_message(message)
        except BaseException as exc:  # pragma: no cover - background failure path
            self._fail_pending(exc)

    def _drain_stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-8]

    @staticmethod
    def _read_message(stream) -> dict[str, Any] | None:
        while True:
            line = stream.readline()
            if not line:
                return None
            stripped = line.strip()
            if stripped:
                break

        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except Exception as exc:
                raise RuntimeError(f"invalid MCP header: {stripped!r}") from exc
            while True:
                header = stream.readline()
                if not header or not header.strip():
                    break
            payload = stream.read(length)
        else:
            payload = stripped

        if not payload:
            return None
        decoded = payload.decode("utf-8", errors="replace")
        value = json.loads(decoded)
        return value if isinstance(value, dict) else None


class ExternalMcpToolServer(BaseToolServer):
    """A stdio MCP server exposed through PuPu's in-process registry."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.raw_name = str(config.get("name") or "").strip()
        self.command = str(config.get("command") or "").strip()
        self.args = [str(item) for item in config.get("args", []) if str(item).strip()]
        self.cwd = str(config.get("cwd") or "").strip() or None
        self.env = {
            str(key): str(value)
            for key, value in (config.get("env") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self.timeout = _coerce_timeout(config.get("timeout"))
        self.exposures = frozenset(_coerce_exposures(config.get("exposures")))
        self._tool_specs: tuple[ToolSpec, ...] | None = None
        super().__init__(
            name=_safe_name(self.raw_name),
            description=str(config.get("description") or "External MCP server."),
            provider="external_mcp",
        )
        self._session = PersistentMcpStdioSession(
            name=self.name,
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
            timeout=self.timeout,
        )

    def close(self) -> None:
        self._session.close()

    def list_tools(self) -> tuple[ToolSpec, ...]:
        if self._tool_specs is None:
            self._tool_specs = self._load_tools()
        return self._tool_specs

    def _load_tools(self) -> tuple[ToolSpec, ...]:
        result = self._request_with_restart("tools/list", {})
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raw_tools = []

        specs: list[ToolSpec] = []
        for item in raw_tools:
            if not isinstance(item, dict):
                continue
            raw_tool_name = str(item.get("name") or "").strip()
            if not raw_tool_name:
                continue
            exposed_tool_name = _safe_name(raw_tool_name, default="tool")
            input_schema = item.get("inputSchema") or item.get("input_schema") or {}
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object"}
            description = str(item.get("description") or "").strip()
            specs.append(
                ToolSpec(
                    server=self.name,
                    name=exposed_tool_name,
                    description=description or f"External MCP tool {raw_tool_name}.",
                    input_schema=input_schema,
                    handler=self._make_handler(raw_tool_name),
                    exposures=self.exposures,
                )
            )
        return tuple(specs)

    def _make_handler(self, raw_tool_name: str):
        def _handler(tool_input: dict, _context: ToolContext) -> ToolResult:
            result = self._request_with_restart(
                "tools/call",
                {"name": raw_tool_name, "arguments": tool_input or {}},
            )
            if result.get("isError"):
                raise RuntimeError(_mcp_text_from_content(result.get("content")))
            return _mcp_text_from_content(result.get("content"))

        return _handler

    def _request_with_restart(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._session.request(method, params)
        except Exception:
            self._session.restart()
            return self._session.request(method, params)


def _coerce_timeout(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MCP_TIMEOUT_SECONDS
    return max(1.0, min(number, 300.0))


def _coerce_exposures(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        items = ["chat"]
    clean = tuple(item for item in items if item)
    return clean or ("chat",)


def build_external_mcp_servers() -> list[ExternalMcpToolServer]:
    servers: list[ExternalMcpToolServer] = []
    for config in external_mcp_server_configs_from_env():
        name = str(config.get("name") or "").strip()
        command = str(config.get("command") or "").strip()
        if not name or not command:
            continue
        if name == "pupu":
            continue
        try:
            server = ExternalMcpToolServer(config)
            server.list_tools()
        except Exception as exc:
            print(f"[pupu][mcp] skip external server {name!r}: {exc}")
            try:
                server.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            continue
        servers.append(server)
    return servers


_ACTIVE_SESSIONS: set[PersistentMcpStdioSession] = set()
atexit.register(shutdown_external_mcp_sessions)

