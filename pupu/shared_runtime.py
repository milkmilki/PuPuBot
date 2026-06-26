"""Process-local shared runtimes for tools and open-group arbitration."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


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


_TOOL_RUNTIME = SharedToolRuntime()


def get_shared_tool_runtime() -> SharedToolRuntime:
    return _TOOL_RUNTIME


def shutdown_shared_runtime() -> None:
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(async_shutdown_shared_runtime())
            return
        if loop.is_running():
            loop.create_task(async_shutdown_shared_runtime())
        else:
            loop.run_until_complete(async_shutdown_shared_runtime())
    except Exception:
        pass


async def async_shutdown_shared_runtime() -> None:
    _TOOL_RUNTIME.shutdown_external_mcp_servers()
    try:
        from .arbiter_runtime import close_shared_arbiter_runtime

        await close_shared_arbiter_runtime()
    except Exception:
        pass


__all__ = [
    "SharedToolRuntime",
    "async_shutdown_shared_runtime",
    "get_shared_tool_runtime",
    "shutdown_shared_runtime",
]
