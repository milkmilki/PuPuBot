"""Global ``pupu.yaml`` loading and environment mapping.

The public configuration surface is intentionally small: users edit one
``pupu.yaml`` file, while runtime modules continue to read the environment
variables they already understand.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in requirements
    yaml = None

APP_CONFIG_ENV_NAMES = ("PUPU_YAML_PATH", "PUPU_CONFIG_FILE")


def get_repo_root() -> Path:
    env = os.environ.get("PUPU_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def get_app_config_path() -> Path:
    for name in APP_CONFIG_ENV_NAMES:
        raw = os.environ.get(name, "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return get_repo_root() / "pupu.yaml"


def get_app_config_example_path() -> Path:
    return get_repo_root() / "pupu.yaml.example"


def ensure_app_config_file(path: str | Path | None = None) -> tuple[Path, bool]:
    """Create ``pupu.yaml`` from the public template if it is missing.

    Returns ``(path, created)``. Existing local config files are never
    overwritten because they may contain private API keys.
    """
    cfg_path = Path(path).expanduser().resolve() if path else get_app_config_path()
    if cfg_path.is_file():
        return cfg_path, False

    template_path = get_app_config_example_path()
    if not template_path.is_file():
        raise FileNotFoundError(f"missing config template: {template_path}")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
    return cfg_path, True


def load_app_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path).expanduser().resolve() if path else get_app_config_path()
    if not cfg_path.is_file():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to read pupu.yaml; install requirements.txt")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _lookup(data: dict[str, Any], dotted_key: str) -> Any:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, set)) and not value:
        return True
    return False


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _set_env(name: str, value: Any, *, override: bool) -> None:
    if _is_empty(value):
        return
    if not override and os.environ.get(name, "").strip():
        return
    os.environ[name] = _stringify(value)


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    out: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _as_command_list(value: Any, default: list[str]) -> list[str]:
    items = _as_string_list(value)
    return items or list(default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _default_provider(cfg: dict[str, Any]) -> str:
    return str(_lookup(cfg, "llm.provider") or "").strip()


def _normalize_mcp_servers(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        raw_items = []
        for name, item in value.items():
            if isinstance(item, dict):
                raw_items.append({"name": name, **item})
    elif isinstance(value, list):
        raw_items = [item for item in value if isinstance(item, dict)]
    else:
        raw_items = []

    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not _as_bool(item.get("enabled"), True):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not name or not command:
            continue
        server: dict[str, Any] = {
            "name": name,
            "command": command,
        }
        args = _as_string_list(item.get("args"))
        if args:
            server["args"] = args
        cwd = str(item.get("cwd") or "").strip()
        if cwd:
            server["cwd"] = cwd
        timeout = str(item.get("timeout") or "").strip()
        if timeout:
            server["timeout"] = timeout
        exposures = _as_string_list(item.get("exposures"))
        if exposures:
            server["exposures"] = exposures
        env = item.get("env")
        if isinstance(env, dict):
            clean_env = {
                str(key).strip(): str(value).strip()
                for key, value in env.items()
                if str(key).strip() and str(value).strip()
            }
            if clean_env:
                server["env"] = clean_env
        out.append(server)
    return out


def apply_app_config_env(
    *,
    override: bool = False,
    path: str | Path | None = None,
    ensure_file: bool = False,
    refresh_tools: bool = True,
) -> dict[str, Any]:
    """Load ``pupu.yaml`` and map supported keys to environment variables.

    Explicit process environment variables win by default. Entry points call
    this early; tests or one-off scripts may pass ``override=True`` when they
    deliberately want the YAML file to replace current environment values.
    """

    if ensure_file:
        ensure_app_config_file(path)
    cfg = load_app_config(path)
    provider = _default_provider(cfg)
    if provider:
        for env_name in (
            "PUPU_CHAT_PROVIDER",
            "PUPU_JUDGE_PROVIDER",
            "PUPU_MAINTENANCE_PROVIDER",
            "PUPU_PROACTIVE_PROVIDER",
        ):
            _set_env(env_name, provider, override=override)

    mapping = {
        "llm.chat_provider": "PUPU_CHAT_PROVIDER",
        "llm.judge_provider": "PUPU_JUDGE_PROVIDER",
        "llm.maintenance_provider": "PUPU_MAINTENANCE_PROVIDER",
        "llm.proactive_provider": "PUPU_PROACTIVE_PROVIDER",
        "llm.anthropic.api_key": "ANTHROPIC_API_KEY",
        "llm.anthropic.base_url": "ANTHROPIC_BASE_URL",
        "llm.anthropic.model": "PUPU_MODEL",
        "llm.anthropic.judge_model": "PUPU_JUDGE_MODEL",
        "llm.anthropic.timeout": "PUPU_ANTHROPIC_TIMEOUT",
        "llm.deepseek.api_key": "PUPU_DEEPSEEK_API_KEY",
        "llm.deepseek.base_url": "PUPU_DEEPSEEK_BASE_URL",
        "llm.deepseek.model": "PUPU_DEEPSEEK_MODEL",
        "llm.deepseek.effort": "PUPU_DEEPSEEK_EFFORT",
        "llm.deepseek.timeout": "PUPU_DEEPSEEK_TIMEOUT",
        "llm.deepseek.temperature": "PUPU_DEEPSEEK_TEMPERATURE",
        "llm.xiaoshuoai.api_key": "PUPU_XIAOSHUOAI_API_KEY",
        "llm.xiaoshuoai.base_url": "PUPU_XIAOSHUOAI_BASE_URL",
        "llm.xiaoshuoai.model": "PUPU_XIAOSHUOAI_MODEL",
        "llm.xiaoshuoai.timeout": "PUPU_XIAOSHUOAI_TIMEOUT",
        "llm.xiaoshuoai.temperature": "PUPU_XIAOSHUOAI_TEMPERATURE",
        "llm.codex_cli.bin": "PUPU_CODEX_BIN",
        "llm.codex_cli.profile": "PUPU_CODEX_PROFILE",
        "llm.codex_cli.reasoning_effort": "PUPU_CODEX_REASONING_EFFORT",
        "llm.codex_cli.tool_mode": "PUPU_CODEX_TOOL_MODE",
        "llm.codex_cli.proxy": "PUPU_CODEX_PROXY",
        "llm.codex_cli.no_proxy": "PUPU_CODEX_NO_PROXY",
        "console.host": "PUPU_CONSOLE_HOST",
        "console.port": "PUPU_CONSOLE_PORT",
        "arbiter.audit": "PUPU_ARBITER_AUDIT",
        "memu.enabled": "PUPU_MEMU_ENABLED",
        "memu.api_key": "PUPU_MEMU_API_KEY",
        "memu.base_url": "PUPU_MEMU_BASE_URL",
        "memu.method": "PUPU_MEMU_METHOD",
        "memu.retrieve_top_k": "PUPU_MEMU_RETRIEVE_TOP_K",
        "memu.ranking": "PUPU_MEMU_RANKING",
        "memu.llm_provider": "PUPU_MEMU_LLM_PROVIDER",
        "memu.llm_api_key": "PUPU_MEMU_LLM_API_KEY",
        "memu.llm_base_url": "PUPU_MEMU_LLM_BASE_URL",
        "memu.llm_model": "PUPU_MEMU_LLM_MODEL",
        "memu.embed_provider": "PUPU_MEMU_EMBED_PROVIDER",
        "memu.embed_api_key": "PUPU_MEMU_EMBED_API_KEY",
        "memu.embed_base_url": "PUPU_MEMU_EMBED_BASE_URL",
        "memu.embed_model": "PUPU_MEMU_EMBED_MODEL",
        "vision.api_key": "PUPU_VISION_API_KEY",
        "vision.base_url": "PUPU_VISION_BASE_URL",
        "vision.model": "PUPU_VISION_MODEL",
        "vision.timeout": "PUPU_VISION_TIMEOUT",
        "tts.enabled": "PUPU_TTS_ENABLED",
        "tts.reply_default": "PUPU_TTS_REPLY_DEFAULT",
        "tts.provider": "PUPU_TTS_PROVIDER",
        "tts.base_url": "PUPU_TTS_BASE_URL",
        "tts.voice": "PUPU_TTS_VOICE",
        "tts.max_chars": "PUPU_TTS_MAX_CHARS",
        "tts.timeout": "PUPU_TTS_TIMEOUT",
        "tts.audio_format": "PUPU_TTS_AUDIO_FORMAT",
        "tts.normalize_audio": "PUPU_TTS_NORMALIZE_AUDIO",
        "tts.ffmpeg": "PUPU_TTS_FFMPEG",
        "runtime.proactive_enabled": "PUPU_PROACTIVE_ENABLED",
        "runtime.debug_scheduled_tasks": "PUPU_DEBUG_SCHEDULED_TASKS",
        "runtime.backup_dir": "PUPU_BACKUP_DIR",
    }
    for dotted_key, env_name in mapping.items():
        _set_env(env_name, _lookup(cfg, dotted_key), override=override)

    mcp_servers = _normalize_mcp_servers(_lookup(cfg, "mcp.servers"))
    if mcp_servers:
        _set_env(
            "PUPU_CODEX_MCP_SERVERS_JSON",
            json.dumps(mcp_servers, ensure_ascii=False),
            override=override,
        )
        _set_env(
            "PUPU_MCP_SERVERS_JSON",
            json.dumps(mcp_servers, ensure_ascii=False),
            override=override,
        )
    elif override:
        os.environ.pop("PUPU_CODEX_MCP_SERVERS_JSON", None)
        os.environ.pop("PUPU_MCP_SERVERS_JSON", None)
    if refresh_tools:
        try:
            from .tools import refresh_tool_definitions

            refresh_tool_definitions()
        except Exception as exc:
            print(f"[pupu][mcp] refresh tools skipped: {exc}")
    return cfg


def default_owner_ids(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config if config is not None else load_app_config()
    return _as_string_list(_lookup(cfg, "user.owner_ids"))


def default_private_allowed_ids(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config if config is not None else load_app_config()
    return _as_string_list(_lookup(cfg, "user.private_allowed_ids"))


def default_private_reply_mode(config: dict[str, Any] | None = None) -> str:
    cfg = config if config is not None else load_app_config()
    mode = str(_lookup(cfg, "user.private_reply_mode") or "owner_only").strip().lower()
    return mode if mode in {"owner_only", "allowlist", "all"} else "owner_only"


def default_napcat_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_app_config()
    return {
        "host": str(_lookup(cfg, "napcat.host") or "0.0.0.0").strip() or "0.0.0.0",
        "port": _as_int(_lookup(cfg, "napcat.port"), 8081),
        "command_start": _as_command_list(_lookup(cfg, "napcat.command_start"), ["/"]),
        "command_sep": _as_command_list(_lookup(cfg, "napcat.command_sep"), ["."]),
        "access_token": str(_lookup(cfg, "napcat.access_token") or "").strip(),
    }

def default_instance_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if config is not None else load_app_config()
    napcat = default_napcat_settings(cfg)
    qq_mode = str(_lookup(cfg, "instance.qq_mode") or "cli").strip().lower() or "cli"
    if qq_mode not in {"cli", "napcat"}:
        qq_mode = "cli"
    return {
        "display_name": str(_lookup(cfg, "instance.display_name") or "仆仆").strip() or "仆仆",
        "qq_mode": qq_mode,
        "owner_ids": default_owner_ids(cfg),
        "private_reply_mode": default_private_reply_mode(cfg),
        "private_allowed_ids": default_private_allowed_ids(cfg),
        "port": napcat["port"],
        "debounce_seconds_open_group": float(_lookup(cfg, "instance.debounce_seconds_open_group") or 35.0),
    }
