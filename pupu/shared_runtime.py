"""Process-local shared runtimes for tools and memU.

This is the middle step before a future single-process actor runtime: current
instances still run as independent processes, while tool and memU objects inside
one process are managed through explicit runtime caches instead of ad-hoc module
globals.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .instance_context import require_current_instance_context


class SharedToolRuntime:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._external_servers: list[Any] | None = None

    def get_external_mcp_servers(self, builder: Callable[[], list[Any]]) -> list[Any]:
        with self._lock:
            if self._external_servers is None:
                self._external_servers = builder()
            return list(self._external_servers)

    def refresh_external_mcp_servers(self, builder: Callable[[], list[Any]]) -> list[Any]:
        with self._lock:
            self.shutdown_external_mcp_servers()
            self._external_servers = builder()
            return list(self._external_servers)

    def shutdown_external_mcp_servers(self) -> None:
        with self._lock:
            servers = self._external_servers or []
            self._external_servers = None
        for server in servers:
            try:
                close = getattr(server, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass


@dataclass(slots=True)
class _MemuEntry:
    service: Any | None = None
    error: str | None = None
    config_signature: tuple[Any, ...] = ()


class SharedMemuRuntime:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], _MemuEntry] = {}

    def current_key(self, memu_db_path: Path | str) -> tuple[str, str]:
        ctx = require_current_instance_context()
        instance_id = ctx.instance_id
        return instance_id, str(Path(memu_db_path).expanduser().resolve())

    def get_service(
        self,
        *,
        memu_db_path: Path | str,
        config_signature: tuple[Any, ...],
        enabled: Callable[[], bool],
        factory: Callable[[], Any],
    ) -> Any:
        key = self.current_key(memu_db_path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.config_signature != config_signature:
                entry = None
                self._entries.pop(key, None)
            if entry is not None and entry.service is not None:
                return entry.service
            if entry is not None and entry.error is not None:
                raise RuntimeError(entry.error)
            if not enabled():
                raise RuntimeError("memU long-term memory is disabled")
            try:
                service = factory()
            except Exception as exc:
                self._entries[key] = _MemuEntry(
                    service=None,
                    error=str(exc),
                    config_signature=config_signature,
                )
                raise
            self._entries[key] = _MemuEntry(
                service=service,
                error=None,
                config_signature=config_signature,
            )
            return service

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_TOOL_RUNTIME = SharedToolRuntime()
_MEMU_RUNTIME = SharedMemuRuntime()


def get_shared_tool_runtime() -> SharedToolRuntime:
    return _TOOL_RUNTIME


def get_shared_memu_runtime() -> SharedMemuRuntime:
    return _MEMU_RUNTIME


def shutdown_shared_runtime() -> None:
    _TOOL_RUNTIME.shutdown_external_mcp_servers()
    _MEMU_RUNTIME.clear()


__all__ = [
    "SharedMemuRuntime",
    "SharedToolRuntime",
    "get_shared_memu_runtime",
    "get_shared_tool_runtime",
    "shutdown_shared_runtime",
]
