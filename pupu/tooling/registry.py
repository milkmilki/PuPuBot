"""Registry and dispatch for MCP-style tool servers."""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

from .base import BaseToolServer, ToolContext, ToolSpec, ToolResult
from .config import load_builtin_server_state
from .external_mcp import build_external_mcp_servers, shutdown_external_mcp_sessions
from .servers import get_builtin_servers


class ToolRegistry:
    """Central registry for server-scoped tools."""

    def __init__(self, servers: Iterable[BaseToolServer]) -> None:
        self._servers: "OrderedDict[str, BaseToolServer]" = OrderedDict()
        self._tools: "OrderedDict[str, ToolSpec]" = OrderedDict()
        self._aliases: dict[str, str] = {}

        for server in servers:
            self.register_server(server)

    def register_server(self, server: BaseToolServer) -> None:
        if server.name in self._servers:
            raise ValueError(f"duplicate tool server: {server.name}")

        self._servers[server.name] = server
        for tool in server.list_tools():
            qualified = tool.qualified_name
            if qualified in self._tools:
                raise ValueError(f"duplicate tool name: {qualified}")
            self._tools[qualified] = tool
            for alias in (qualified, tool.name, *tool.legacy_names):
                existing = self._aliases.get(alias)
                if existing and existing != qualified:
                    raise ValueError(f"duplicate tool alias: {alias}")
                self._aliases[alias] = qualified

    def resolve(self, name: str) -> ToolSpec:
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
    ) -> ToolResult:
        tool = self.resolve(name)
        return tool.handler(tool_input, context or ToolContext())

    def describe_servers(self) -> list[dict[str, str | int]]:
        out: list[dict[str, str | int]] = []
        for server in self._servers.values():
            out.append(
                {
                    "name": server.name,
                    "provider": server.provider,
                    "tool_count": len(server.list_tools()),
                    "description": server.description,
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
    servers.extend(build_external_mcp_servers())
    return ToolRegistry(servers)


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def refresh_registry() -> ToolRegistry:
    global _registry
    shutdown_external_mcp_sessions()
    _registry = build_registry()
    return _registry
