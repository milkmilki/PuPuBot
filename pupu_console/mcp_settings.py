"""Console-facing MCP settings manifest and config helpers."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from pupu.app_config import apply_app_config_env, ensure_app_config_file, load_app_config
from pupu.tooling.config import DEFAULT_BUILTIN_SERVER_STATE
from pupu.tooling.external_mcp import ExternalMcpToolServer
from pupu.tooling.servers import get_builtin_servers

BUILTIN_LABELS = {
    "media": "图片识别",
    "scheduler": "定时任务",
    "filesystem": "文件系统",
    "system": "系统命令",
    "web": "网页工具",
}

BUILTIN_CONFIG_FIELDS = {
    "media": [
        {
            "key": "semantic_index.embed_api_key",
            "label": "百炼 API Key",
            "type": "secret",
            "placeholder": "复用语义索引/百炼 key",
        },
        {
            "key": "semantic_index.embed_base_url",
            "label": "百炼 Base URL",
            "type": "text",
            "placeholder": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
        {
            "key": "vision.model",
            "label": "视觉模型",
            "type": "text",
            "placeholder": "qwen3.6-flash",
        },
        {
            "key": "vision.timeout",
            "label": "超时秒数",
            "type": "number",
            "placeholder": "45",
        },
    ],
}

SECRET_FIELD_KEYS = {
    "semantic_index.embed_api_key",
}

EXTERNAL_SECRET_NAMES = {
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
}

TAVILY_PRESET = {
    "name": "tavily",
    "display_name": "Web Search / Tavily",
    "description": "Tavily MCP server: web search, extract, crawl, map, and research.",
    "enabled": False,
    "command": "cmd",
    "args": ["/c", "npx", "-y", "tavily-mcp@latest"],
    "exposures": ["chat", "proactive"],
    "timeout": 30,
    "env": {"TAVILY_API_KEY": ""},
}


def nested_get(data: dict[str, Any], dotted_key: str) -> Any:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def nested_set(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    cur: Any = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = cur.get(part)
        if not isinstance(child, dict):
            child = {}
            cur[part] = child
        cur = child
    cur[parts[-1]] = value


def mask_secret(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {"configured": False, "has_value": False, "preview": "", "masked": ""}
    suffix = text[-4:] if len(text) >= 4 else text
    masked = f"****{suffix}"
    return {"configured": True, "has_value": True, "preview": masked, "masked": masked}


def ensure_config() -> tuple[Path, dict[str, Any]]:
    config_path, _ = ensure_app_config_file()
    return config_path, load_app_config(config_path)


def write_config(config_path: Path, cfg: dict[str, Any]) -> None:
    from pupu import app_config

    if app_config.yaml is None:
        raise RuntimeError("PyYAML is required to write pupu.yaml")
    config_path.write_text(
        app_config.yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _is_secret_env_name(name: str) -> bool:
    upper = str(name or "").upper()
    return any(token in upper for token in EXTERNAL_SECRET_NAMES)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return [str(item).strip() for item in raw if str(item).strip()]


def _server_map(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        out: dict[str, dict[str, Any]] = {}
        for name, item in raw.items():
            if isinstance(item, dict):
                current = dict(item)
                current.setdefault("name", str(name))
                out[str(name)] = current
        return out
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                out[name] = dict(item)
        return out
    return {}


def _configured_builtin_state(cfg: dict[str, Any]) -> dict[str, bool]:
    state = dict(DEFAULT_BUILTIN_SERVER_STATE)
    raw = cfg.get("tool_servers")
    if isinstance(raw, dict):
        for name in state:
            item = raw.get(name)
            if isinstance(item, bool):
                state[name] = item
            elif isinstance(item, dict) and "enabled" in item:
                state[name] = bool(item["enabled"])
    return state


def _current_registry_servers() -> dict[str, dict[str, Any]]:
    try:
        from pupu.tools import describe_tool_servers

        return {str(item.get("name")): dict(item) for item in describe_tool_servers()}
    except Exception:
        return {}


def _tool_summary_from_server(server) -> list[dict[str, Any]]:
    tools = []
    try:
        specs = server.list_tools()
    except Exception:
        specs = ()
    for spec in specs:
        tools.append(
            {
                "name": spec.qualified_name,
                "raw_name": spec.name,
                "description": spec.description,
                "exposures": sorted(spec.exposures),
                "admin_only": bool(spec.admin_only),
            }
        )
    return tools


def _config_field_value(cfg: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
    key = str(field.get("key") or "")
    value = nested_get(cfg, key)
    out = dict(field)
    if field.get("type") == "secret" or key in SECRET_FIELD_KEYS:
        out["secret"] = mask_secret(value)
        out["value"] = ""
    else:
        out["value"] = "" if value is None else str(value)
    return out


def build_mcp_settings_payload() -> dict[str, Any]:
    config_path, cfg = ensure_config()
    builtin_state = _configured_builtin_state(cfg)
    registry = _current_registry_servers()
    builtin_by_name = {server.name: server for server in get_builtin_servers()}

    builtin_servers: list[dict[str, Any]] = []
    for name in sorted(DEFAULT_BUILTIN_SERVER_STATE):
        server = builtin_by_name.get(name)
        registry_info = registry.get(name, {})
        config_fields = [
            _config_field_value(cfg, field)
            for field in BUILTIN_CONFIG_FIELDS.get(name, [])
        ]
        tools = _tool_summary_from_server(server) if server is not None else []
        builtin_servers.append(
            {
                "id": name,
                "name": name,
                "display_name": BUILTIN_LABELS.get(name, name),
                "kind": "builtin",
                "provider": "builtin",
                "installed": server is not None,
                "enabled": bool(builtin_state.get(name, True)),
                "loaded": bool(registry_info),
                "description": (server.description if server is not None else ""),
                "tool_count": int(registry_info.get("tool_count") or (len(tools) if builtin_state.get(name, True) else 0)),
                "tools": tools if builtin_state.get(name, True) else [],
                "config_fields": config_fields,
                "status": "loaded" if registry_info else ("disabled" if not builtin_state.get(name, True) else "unavailable"),
                "error": "",
            }
        )

    configured_servers = _server_map(nested_get(cfg, "mcp.servers"))

    external_servers: list[dict[str, Any]] = []
    for name in sorted(configured_servers):
        item = configured_servers[name]
        enabled = bool(item.get("enabled", True))
        registry_info = registry.get(name, {})
        env = item.get("env") if isinstance(item.get("env"), dict) else {}
        env_fields = []
        for env_name in sorted(str(key) for key in env):
            value = env.get(env_name)
            if _is_secret_env_name(env_name):
                env_fields.append({"name": env_name, "type": "secret", "secret": mask_secret(value), "value": ""})
            else:
                env_fields.append({"name": env_name, "type": "text", "value": "" if value is None else str(value)})
        external_servers.append(
            {
                "id": name,
                "name": name,
                "display_name": str(item.get("display_name") or item.get("description") or name),
                "kind": "external",
                "provider": "external_mcp",
                "installed": bool(str(item.get("command") or "").strip()),
                "enabled": enabled,
                "loaded": bool(registry_info),
                "description": str(item.get("description") or ""),
                "command": str(item.get("command") or ""),
                "args": _string_list(item.get("args")),
                "cwd": str(item.get("cwd") or ""),
                "timeout": str(item.get("timeout") or ""),
                "exposures": _string_list(item.get("exposures")) or ["chat"],
                "env": env_fields,
                "tool_count": int(registry_info.get("tool_count") or 0),
                "tools": registry_info.get("tools") or [],
                "config_fields": [],
                "status": "loaded" if registry_info else ("disabled" if not enabled else "configured"),
                "error": "",
                "preset": name == "tavily",
            }
        )

    return {
        "config_path": str(config_path),
        "builtin_servers": builtin_servers,
        "external_servers": external_servers,
        "presets": [deepcopy(TAVILY_PRESET)],
    }


def _coerce_external_input(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or item.get("id") or "").strip()
    if not name:
        return None
    if item.get("preset") and name == "tavily":
        preset = deepcopy(TAVILY_PRESET)
        preset["enabled"] = bool(item.get("enabled", preset.get("enabled", False)))
        env_in = item.get("env")
        env: dict[str, str] = dict(preset.get("env") or {})
        if isinstance(env_in, dict):
            for key, value in env_in.items():
                env_name = str(key).strip()
                env_value = str(value or "").strip()
                if env_name and env_value:
                    env[env_name] = env_value
        elif isinstance(env_in, list):
            for field in env_in:
                if not isinstance(field, dict):
                    continue
                env_name = str(field.get("name") or "").strip()
                env_value = str(field.get("value") or "").strip()
                if env_name and env_value:
                    env[env_name] = env_value
        preset["env"] = {key: value for key, value in env.items() if str(value).strip()}
        return preset
    server: dict[str, Any] = {
        "enabled": bool(item.get("enabled", True)),
        "command": str(item.get("command") or "").strip(),
    }
    args = _string_list(item.get("args"))
    if args:
        server["args"] = args
    cwd = str(item.get("cwd") or "").strip()
    if cwd:
        server["cwd"] = cwd
    timeout = str(item.get("timeout") or "").strip()
    if timeout:
        server["timeout"] = timeout
    exposures = _string_list(item.get("exposures"))
    if exposures:
        server["exposures"] = exposures
    description = str(item.get("description") or "").strip()
    if description:
        server["description"] = description
    env_in = item.get("env")
    env: dict[str, str] = {}
    if isinstance(env_in, dict):
        for key, value in env_in.items():
            env_name = str(key).strip()
            env_value = str(value or "").strip()
            if env_name and env_value:
                env[env_name] = env_value
    elif isinstance(env_in, list):
        for field in env_in:
            if not isinstance(field, dict):
                continue
            env_name = str(field.get("name") or "").strip()
            env_value = str(field.get("value") or "").strip()
            if env_name and env_value:
                env[env_name] = env_value
    if env:
        server["env"] = env
    return {"name": name, **server}


def _redact_configured_secrets(text: str, item: dict[str, Any]) -> str:
    redacted = text
    env = item.get("env")
    if isinstance(env, dict):
        for value in env.values():
            secret = str(value or "").strip()
            if secret:
                redacted = redacted.replace(secret, "[secret]")
    for key in ("api_key", "token", "secret", "password"):
        secret = str(item.get(key) or "").strip()
        if secret:
            redacted = redacted.replace(secret, "[secret]")
    return redacted


def update_mcp_settings(body: dict[str, Any]) -> dict[str, Any]:
    config_path, cfg = ensure_config()

    builtin_updates = body.get("builtin_servers")
    if isinstance(builtin_updates, list):
        current = cfg.get("tool_servers")
        if not isinstance(current, dict):
            current = {}
        for item in builtin_updates:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            if name not in DEFAULT_BUILTIN_SERVER_STATE:
                continue
            existing = current.get(name)
            if not isinstance(existing, dict):
                existing = {}
            if "enabled" in item:
                existing["enabled"] = bool(item.get("enabled"))
            current[name] = existing
        cfg["tool_servers"] = current

    values = body.get("values")
    if isinstance(values, dict):
        for key, raw in values.items():
            dotted = str(key)
            if dotted not in {
                "semantic_index.embed_api_key",
                "semantic_index.embed_base_url",
                "vision.model",
                "vision.timeout",
            }:
                continue
            value = str(raw or "").strip()
            if dotted in SECRET_FIELD_KEYS and not value:
                continue
            nested_set(cfg, dotted, value)

    if body.get("delete_external"):
        delete_names = {str(name).strip() for name in _string_list(body.get("delete_external"))}
    else:
        delete_names = set()
    external_updates = body.get("external_servers")
    if isinstance(external_updates, list) or delete_names:
        current_servers = _server_map(nested_get(cfg, "mcp.servers"))
        for name in delete_names:
            current_servers.pop(name, None)
        if isinstance(external_updates, list):
            for item in external_updates:
                if not isinstance(item, dict):
                    continue
                normalized = _coerce_external_input(item)
                if normalized is None:
                    continue
                name = normalized.pop("name")
                existing = current_servers.get(name, {})
                if isinstance(existing.get("env"), dict) and "env" not in normalized:
                    normalized["env"] = existing["env"]
                elif isinstance(existing.get("env"), dict) and isinstance(normalized.get("env"), dict):
                    merged_env = dict(existing["env"])
                    merged_env.update(normalized["env"])
                    normalized["env"] = {k: v for k, v in merged_env.items() if str(v).strip()}
                current_servers[name] = normalized
        nested_set(cfg, "mcp.servers", current_servers)

    write_config(config_path, cfg)
    apply_app_config_env(override=True, refresh_tools=False)
    return build_mcp_settings_payload()


def refresh_mcp_settings() -> dict[str, Any]:
    apply_app_config_env(override=True)
    return build_mcp_settings_payload()


def test_mcp_server(server_id: str) -> dict[str, Any]:
    _, cfg = ensure_config()
    server_id = str(server_id or "").strip()
    if not server_id:
        return {"ok": False, "server_id": "", "error": "missing server_id", "tools": []}

    if server_id in DEFAULT_BUILTIN_SERVER_STATE:
        builtin = {server.name: server for server in get_builtin_servers()}.get(server_id)
        if builtin is None:
            return {"ok": False, "server_id": server_id, "error": "builtin server not installed", "tools": []}
        return {"ok": True, "server_id": server_id, "tools": _tool_summary_from_server(builtin), "error": ""}

    configured = _server_map(nested_get(cfg, "mcp.servers"))
    item = configured.get(server_id)
    if item is None and server_id == "tavily":
        item = deepcopy(TAVILY_PRESET)
    if not isinstance(item, dict):
        return {"ok": False, "server_id": server_id, "error": "unknown MCP server", "tools": []}
    if not item.get("enabled", True):
        return {"ok": False, "server_id": server_id, "error": "MCP server is disabled", "tools": []}
    if not str(item.get("command") or "").strip():
        return {"ok": False, "server_id": server_id, "error": "missing command", "tools": []}

    server: ExternalMcpToolServer | None = None
    try:
        server = ExternalMcpToolServer({"name": server_id, **item})
        tools = []
        for spec in server.list_tools():
            tools.append(
                {
                    "name": spec.qualified_name,
                    "raw_name": spec.name,
                    "description": spec.description,
                    "exposures": sorted(spec.exposures),
                    "admin_only": bool(spec.admin_only),
                }
            )
        return {"ok": True, "server_id": server_id, "tools": tools, "error": ""}
    except Exception as exc:
        error = str(exc).replace("\r", " ").replace("\n", " ").strip()
        error = _redact_configured_secrets(error, item)
        return {"ok": False, "server_id": server_id, "tools": [], "error": error[:500]}
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
