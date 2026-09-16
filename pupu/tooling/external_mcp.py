"""External stdio MCP integration backed by the official Python SDK."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client
from mcp import types as mcp_types

from ..model_runtime import ToolResult, remaining_agent_timeout
from .base import BaseToolServer, ToolContext, ToolResultValue, ToolSpec

DEFAULT_MCP_TIMEOUT_SECONDS = 25.0
DEFAULT_MCP_STDERR_MAX_BYTES = 64 * 1024


def external_mcp_server_configs_from_env() -> list[dict[str, Any]]:
    """Load normalized external MCP server configs from the process env."""

    raw = os.environ.get("PUPU_MCP_SERVERS_JSON", "").strip()
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


def _redact_config_secrets(text: str, config: dict[str, Any]) -> str:
    redacted = text
    env = config.get("env")
    if isinstance(env, dict):
        for value in env.values():
            secret = str(value or "").strip()
            if secret:
                redacted = redacted.replace(secret, "[secret]")
    for key in ("api_key", "token", "secret", "password"):
        secret = str(config.get(key) or "").strip()
        if secret:
            redacted = redacted.replace(secret, "[secret]")
    return redacted


@dataclass(slots=True)
class _SessionCommand:
    operation: str
    arguments: tuple[Any, ...]
    future: Future[Any]
    timeout_seconds: float


@dataclass(slots=True)
class _StartupAttempt:
    ready: threading.Event
    error: BaseException | None = None


class _WorkerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CLOSED = "closed"


class _BoundedTextLog:
    """A file-descriptor-backed stderr sink retaining only a bounded tail."""

    def __init__(self, max_bytes: int = DEFAULT_MCP_STDERR_MAX_BYTES) -> None:
        self._max_bytes = max(1024, int(max_bytes))
        self._read_fd, write_fd = os.pipe()
        self._writer = os.fdopen(write_fd, "w", encoding="utf-8", buffering=1)
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(
            target=self._drain,
            name="pupu-mcp-stderr",
            daemon=True,
        )
        self._reader.start()

    def fileno(self) -> int:
        return self._writer.fileno()

    def write(self, text: str) -> int:
        return self._writer.write(text)

    def flush(self) -> None:
        self._writer.flush()

    @property
    def closed(self) -> bool:
        return self._closed

    def tail(self, max_lines: int = 8) -> str:
        with self._lock:
            text = bytes(self._buffer).decode("utf-8", errors="replace")
        return " | ".join(text.splitlines()[-max(1, max_lines) :])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except Exception:
            pass
        self._reader.join(timeout=1.0)
        try:
            os.close(self._read_fd)
        except OSError:
            pass

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._read_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._buffer.extend(chunk)
                overflow = len(self._buffer) - self._max_bytes
                if overflow > 0:
                    del self._buffer[:overflow]


class PersistentMcpStdioSession:
    """Persistent synchronous facade over the official async MCP client."""

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
        self.args = list(args)
        self.env = dict(env)
        self.cwd = cwd
        self.timeout = timeout
        self._commands: queue.Queue[_SessionCommand] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._startup: _StartupAttempt | None = None
        self._state = _WorkerState.STOPPED
        self._tools_changed = threading.Event()
        self._closed = False
        self._state_changed = threading.Condition(threading.RLock())
        self._stderr = _BoundedTextLog()
        self.protocol_version = ""
        self._proc = None
        _ACTIVE_SESSIONS.add(self)

    @property
    def stderr_tail(self) -> str:
        try:
            return self._stderr.tail()
        except Exception:
            return ""

    def ensure_started(self) -> None:
        wait_timeout = remaining_agent_timeout(self.timeout + 5.0)
        wait_deadline = time.monotonic() + (
            wait_timeout if wait_timeout is not None else self.timeout + 5.0
        )
        while True:
            with self._state_changed:
                if self._closed:
                    raise RuntimeError(f"MCP session {self.name!r} is closed")
                if (
                    self._state == _WorkerState.RUNNING
                    and self._thread is not None
                    and self._thread.is_alive()
                ):
                    return
                if self._state == _WorkerState.STARTING and self._startup is not None:
                    startup = self._startup
                elif self._state == _WorkerState.STOPPING:
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"MCP session {self.name!r} restart timed out"
                        )
                    self._state_changed.wait(timeout=remaining)
                    continue
                else:
                    startup = _StartupAttempt(ready=threading.Event())
                    commands: queue.Queue[_SessionCommand] = queue.Queue()
                    self._commands = commands
                    self._startup = startup
                    self._state = _WorkerState.STARTING
                    self._thread = threading.Thread(
                        target=self._thread_main,
                        args=(startup, commands),
                        name=f"pupu-mcp-{self.name}",
                        daemon=True,
                    )
                    self._thread.start()

            remaining = wait_deadline - time.monotonic()
            if remaining <= 0 or not startup.ready.wait(timeout=remaining):
                self._stop_worker()
                raise TimeoutError(f"MCP session {self.name!r} startup timed out")
            if startup.error is not None:
                raise RuntimeError(
                    f"MCP session {self.name!r} failed: {startup.error}"
                ) from startup.error

    def close(self) -> None:
        with self._state_changed:
            if self._closed:
                return
            self._closed = True
        self._stop_worker()
        _ACTIVE_SESSIONS.discard(self)
        try:
            self._stderr.close()
        except Exception:
            pass
        self._close_legacy_process()

    def restart(self) -> None:
        with self._state_changed:
            if self._closed:
                raise RuntimeError(f"MCP session {self.name!r} is closed")
        self._stop_worker()
        self.ensure_started()

    def list_tools(self) -> list[dict[str, Any]]:
        try:
            return self._submit("list_tools")
        except Exception:
            self.restart()
            return self._submit("list_tools")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._submit("call_tool", name, arguments)
        except Exception:
            # The server may have completed a side effect before the transport
            # failed. Reconnect for the next request, but never replay this call.
            self._stop_worker()
            raise

    def consume_tools_changed(self) -> bool:
        changed = self._tools_changed.is_set()
        self._tools_changed.clear()
        return changed

    def _submit(self, operation: str, *arguments: Any):
        self.ensure_started()
        request_timeout = remaining_agent_timeout(self.timeout)
        if request_timeout is None:
            request_timeout = self.timeout
        future: Future[Any] = Future()
        with self._state_changed:
            if self._state != _WorkerState.RUNNING:
                raise RuntimeError(f"MCP session {self.name!r} is not running")
            self._commands.put(
                _SessionCommand(
                    operation,
                    arguments,
                    future,
                    timeout_seconds=request_timeout,
                )
            )
        try:
            future_timeout = remaining_agent_timeout(request_timeout + 0.5)
            return future.result(
                timeout=future_timeout
                if future_timeout is not None
                else request_timeout + 0.5
            )
        except FutureTimeoutError as exc:
            self._stop_worker()
            raise TimeoutError(f"MCP request timed out: {operation}") from exc

    def _thread_main(
        self,
        startup: _StartupAttempt,
        commands: queue.Queue[_SessionCommand],
    ) -> None:
        error: BaseException | None = None
        try:
            asyncio.run(self._serve(startup, commands))
        except BaseException as exc:
            error = exc
            if not startup.ready.is_set():
                startup.error = exc
                startup.ready.set()
        finally:
            self._fail_pending(
                commands,
                error or RuntimeError(f"MCP session {self.name!r} stopped"),
            )
            with self._state_changed:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._state = (
                        _WorkerState.CLOSED
                        if self._closed
                        else _WorkerState.STOPPED
                    )
                self._state_changed.notify_all()

    async def _serve(
        self,
        startup: _StartupAttempt,
        commands: queue.Queue[_SessionCommand],
    ) -> None:
        parameters = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        transport = stdio_client(parameters, errlog=self._stderr)

        async def _handle_message(message) -> None:
            if isinstance(message, mcp_types.ToolListChangedNotification):
                self._tools_changed.set()

        async with Client(
            transport,
            mode="auto",
            read_timeout_seconds=self.timeout,
            message_handler=_handle_message,
        ) as client:
            self.protocol_version = client.protocol_version
            with self._state_changed:
                if self._startup is startup and self._state == _WorkerState.STARTING:
                    self._state = _WorkerState.RUNNING
                startup.ready.set()
                self._state_changed.notify_all()
            listener = asyncio.create_task(self._listen_for_tool_changes(client))
            try:
                while True:
                    command = await asyncio.to_thread(commands.get)
                    if command.operation == "close":
                        command.future.set_result(None)
                        break
                    try:
                        async with asyncio.timeout(command.timeout_seconds):
                            if command.operation == "list_tools":
                                value = await _list_all_tools(client)
                            elif command.operation == "call_tool":
                                value = await client.call_tool(
                                    command.arguments[0],
                                    command.arguments[1],
                                    read_timeout_seconds=command.timeout_seconds,
                                )
                                value = value.model_dump(
                                    by_alias=True,
                                    mode="json",
                                    exclude_none=True,
                                )
                            else:
                                raise RuntimeError(
                                    f"unknown MCP operation: {command.operation}"
                                )
                    except Exception as exc:
                        command.future.set_exception(exc)
                    else:
                        command.future.set_result(value)
            finally:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener

    async def _listen_for_tool_changes(self, client: Client) -> None:
        try:
            async with client.listen(tools_list_changed=True) as subscription:
                async for _event in subscription:
                    self._tools_changed.set()
        except Exception:
            # Legacy servers and modern servers without subscriptions are
            # already covered by the normal notification callback.
            return

    def _stop_worker(self, timeout_seconds: float = 5.0) -> None:
        wait_timeout = remaining_agent_timeout(timeout_seconds)
        if wait_timeout is None:
            wait_timeout = timeout_seconds
        stop_deadline = time.monotonic() + max(0.0, wait_timeout)
        future: Future[Any] | None = None
        with self._state_changed:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread = None
                self._state = (
                    _WorkerState.CLOSED if self._closed else _WorkerState.STOPPED
                )
                self._state_changed.notify_all()
                return
            if self._state != _WorkerState.STOPPING:
                self._state = _WorkerState.STOPPING
                future = Future()
                self._commands.put(
                    _SessionCommand("close", (), future, timeout_seconds=5.0)
                )
                self._state_changed.notify_all()
        if future is not None:
            try:
                future.result(timeout=max(0.0, stop_deadline - time.monotonic()))
            except Exception:
                pass
        thread.join(timeout=max(0.0, stop_deadline - time.monotonic()))
        with self._state_changed:
            if not thread.is_alive() and self._thread is thread:
                self._thread = None
            if self._thread is None:
                self._state = (
                    _WorkerState.CLOSED if self._closed else _WorkerState.STOPPED
                )
            self._state_changed.notify_all()

    @staticmethod
    def _fail_pending(
        commands: queue.Queue[_SessionCommand],
        exc: BaseException,
    ) -> None:
        while True:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                return
            if not command.future.done():
                command.future.set_exception(exc)

    def _close_legacy_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (
            getattr(proc, "stdin", None),
            getattr(proc, "stdout", None),
            getattr(proc, "stderr", None),
        ):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


async def _list_all_tools(client: Client) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = await client.list_tools(cursor=cursor, cache_mode="bypass")
        tools.extend(
            tool.model_dump(by_alias=True, mode="json", exclude_none=True)
            for tool in page.tools
        )
        cursor = page.next_cursor
        if not cursor:
            return tools
        if cursor in seen_cursors:
            raise RuntimeError("MCP tools/list returned a repeated cursor")
        seen_cursors.add(cursor)


class ExternalMcpToolServer(BaseToolServer):
    """An external stdio MCP server exposed through PuPu's registry."""

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
        if self._tool_specs is None or self._session.consume_tools_changed():
            self._tool_specs = self._load_tools()
        return self._tool_specs

    def tools_changed(self) -> bool:
        if not self._session.consume_tools_changed():
            return False
        self._tool_specs = None
        return True

    def _load_tools(self) -> tuple[ToolSpec, ...]:
        raw_tools = self._session.list_tools()
        specs: list[ToolSpec] = []
        for item in raw_tools:
            raw_tool_name = str(item.get("name") or "").strip()
            if not raw_tool_name:
                continue
            exposed_tool_name = _safe_name(raw_tool_name, default="tool")
            input_schema = item.get("inputSchema") or {}
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object"}
            output_schema = item.get("outputSchema")
            specs.append(
                ToolSpec(
                    server=self.name,
                    name=exposed_tool_name,
                    description=str(item.get("description") or "").strip()
                    or f"External MCP tool {raw_tool_name}.",
                    input_schema=input_schema,
                    output_schema=output_schema
                    if isinstance(output_schema, dict)
                    else None,
                    annotations=item.get("annotations")
                    if isinstance(item.get("annotations"), dict)
                    else None,
                    handler=self._make_handler(raw_tool_name),
                    exposures=self.exposures,
                )
            )
        return tuple(specs)

    def _make_handler(self, raw_tool_name: str):
        def _handler(tool_input: dict, _context: ToolContext) -> ToolResultValue:
            result = self._session.call_tool(raw_tool_name, tool_input or {})
            return _tool_result_from_mcp(result)

        return _handler


def _tool_result_from_mcp(result: dict[str, Any]) -> ToolResultValue:
    raw_content = result.get("content")
    content = (
        [dict(block) for block in raw_content if isinstance(block, dict)]
        if isinstance(raw_content, list)
        else []
    )
    structured = result.get("structuredContent")
    is_error = bool(result.get("isError"))
    if (
        not is_error
        and structured is None
        and all(block.get("type") == "text" for block in content)
    ):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if str(block.get("text") or "").strip()
        )
    if not content and structured is not None:
        content_value: str | list[dict[str, Any]] = json.dumps(
            structured, ensure_ascii=False
        )
    else:
        content_value = content
    return ToolResult(
        content=content_value,
        is_error=is_error,
        structured_content=structured,
    )


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
        if not name or not command or name == "pupu":
            continue
        server: ExternalMcpToolServer | None = None
        try:
            server = ExternalMcpToolServer(config)
            server.list_tools()
        except Exception as exc:
            error = _redact_config_secrets(str(exc), config)
            print(f"[pupu][mcp] skip external server {name!r}: {error}")
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            continue
        servers.append(server)
    return servers


_ACTIVE_SESSIONS: set[PersistentMcpStdioSession] = set()
atexit.register(shutdown_external_mcp_sessions)
