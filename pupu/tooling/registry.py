"""Registry and dispatch for MCP-style tool servers."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable

from ..shared_runtime import get_shared_tool_runtime
from .base import BaseToolServer, ToolContext, ToolResultValue, ToolSpec
from .config import load_builtin_server_state
from .external_mcp import build_external_mcp_servers
from .servers import get_builtin_servers


class ToolRegistry:
    """Central registry for server-scoped tools."""

    def __init__(self, servers: Iterable[BaseToolServer]) -> None:
        self._servers: OrderedDict[str, BaseToolServer] = OrderedDict()
        self._tools: OrderedDict[str, ToolSpec] = OrderedDict()
        self._aliases: dict[str, str] = {}
        self._ambiguous_aliases: set[str] = set()
        self._lock = threading.RLock()

        for server in servers:
            self.register_server(server)

    def register_server(self, server: BaseToolServer) -> None:
        with self._lock:
            if server.name in self._servers:
                raise ValueError(f"duplicate tool server: {server.name}")

            self._servers[server.name] = server
            self._register_tools(server)

    def _register_tools(self, server: BaseToolServer) -> None:
        for tool in server.list_tools():
            qualified = tool.qualified_name
            if qualified in self._tools:
                raise ValueError(f"duplicate tool name: {qualified}")
            self._tools[qualified] = tool
            for alias in (qualified, tool.name, *tool.legacy_names):
                if alias in self._ambiguous_aliases:
                    continue
                existing = self._aliases.get(alias)
                if existing and existing != qualified:
                    self._aliases.pop(alias, None)
                    self._ambiguous_aliases.add(alias)
                    continue
                self._aliases[alias] = qualified

    def _refresh_changed_servers(self) -> None:
        if not any(server.tools_changed() for server in self._servers.values()):
            return
        new_tools: OrderedDict[str, ToolSpec] = OrderedDict()
        old_tools = self._tools
        old_aliases = self._aliases
        old_ambiguous = self._ambiguous_aliases
        self._tools = new_tools
        self._aliases = {}
        self._ambiguous_aliases = set()
        try:
            for server in self._servers.values():
                self._register_tools(server)
        except Exception:
            self._tools = old_tools
            self._aliases = old_aliases
            self._ambiguous_aliases = old_ambiguous
            raise

    def resolve(self, name: str) -> ToolSpec:
        with self._lock:
            self._refresh_changed_servers()
            qualified = self._aliases.get(name, name)
            tool = self._tools.get(qualified)
            if tool is None:
                raise KeyError(name)
            return tool

    def is_admin_tool(self, name: str) -> bool:
        try:
            return self.resolve(name).admin_only
        except KeyError:
            return False

    def get_api_definitions(self, exposure: str = "chat") -> list[dict]:
        with self._lock:
            self._refresh_changed_servers()
            return [
                tool.to_api_definition()
                for tool in self._tools.values()
                if exposure in tool.exposures
            ]

    def execute(
        self,
        name: str,
        tool_input: dict,
        context: ToolContext | None = None,
    ) -> ToolResultValue:
        tool = self.resolve(name)
        return tool.handler(tool_input, context or ToolContext())

    def describe_servers(self) -> list[dict[str, object]]:
        with self._lock:
            self._refresh_changed_servers()
            out: list[dict[str, object]] = []
            for server in self._servers.values():
                tools = []
                for tool in server.list_tools():
                    tools.append(
                        {
                            "name": tool.qualified_name,
                            "raw_name": tool.name,
                            "description": tool.description,
                            "exposures": sorted(tool.exposures),
                            "admin_only": bool(tool.admin_only),
                        }
                    )
                out.append(
                    {
                        "name": server.name,
                        "provider": server.provider,
                        "tool_count": len(tools),
                        "description": server.description,
                        "tools": tools,
                    }
                )
            return out


def build_registry() -> ToolRegistry:
    state = load_builtin_server_state()
    servers = [
        server
        for server in get_builtin_servers()
        if state.get(server.name, True)
    ]
    servers.extend(
        get_shared_tool_runtime().get_external_mcp_servers(build_external_mcp_servers)
    )
    return ToolRegistry(servers)


def build_registry_with_external_servers(external_servers: Iterable[BaseToolServer]) -> ToolRegistry:
    state = load_builtin_server_state()
    servers = [
        server
        for server in get_builtin_servers()
        if state.get(server.name, True)
    ]
    servers.extend(external_servers)
    return ToolRegistry(servers)


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def refresh_registry() -> ToolRegistry:
    global _registry
    external_servers = get_shared_tool_runtime().refresh_external_mcp_servers(
        build_external_mcp_servers
    )
    _registry = build_registry_with_external_servers(external_servers)
    return _registry
