"""Create, read, update, and remove PuPu instance directories."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from pupu.app_config import (
    default_instance_settings,
    default_owner_ids,
    default_napcat_settings,
    write_env_qq_file,
)

from .paths import instances_dir

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Removed from product; strip if still present in older files.
_DEPRECATED_INSTANCE_KEYS = frozenset({"mode", "persona_enabled", "llm"})

DEFAULT_TOOL_SERVERS: dict[str, dict[str, bool]] = {
    "web": {"enabled": True},
    "filesystem": {"enabled": True},
    "system": {"enabled": True},
    "media": {"enabled": True},
    "scheduler": {"enabled": True},
}

DEFAULT_ARBITER_URL = "http://127.0.0.1:18079/api/group_arbitrate"
DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS = 35.0


def validate_instance_id(instance_id: str) -> None:
    if not instance_id or not _ID_RE.match(instance_id):
        raise ValueError("invalid instance id")


def _default_persona() -> dict[str, Any]:
    from pupu.persona.core import get_core_persona, get_pupu_name, get_seed_self_facts

    return {
        "name": get_pupu_name(),
        "core_persona": get_core_persona(),
        "seed_self_facts": get_seed_self_facts(),
    }


def _scrub_deprecated_instance_keys(cfg: dict[str, Any]) -> None:
    for k in _DEPRECATED_INSTANCE_KEYS:
        cfg.pop(k, None)


def _normalize_instance_config(cfg: dict[str, Any]) -> None:
    _scrub_deprecated_instance_keys(cfg)
    cfg.setdefault("open_groups", [])
    if not isinstance(cfg["open_groups"], list):
        cfg["open_groups"] = []
    cfg["open_groups"] = [
        str(value).strip()
        for value in cfg["open_groups"]
        if str(value).strip()
    ]

    cfg.setdefault("bot_id", "")
    cfg["bot_id"] = str(cfg.get("bot_id") or "").strip()

    cfg.setdefault("arbiter_url", DEFAULT_ARBITER_URL)
    cfg["arbiter_url"] = str(cfg.get("arbiter_url") or DEFAULT_ARBITER_URL).strip()

    cfg.setdefault("peer", {})
    if not isinstance(cfg["peer"], dict):
        cfg["peer"] = {}
    peer = cfg["peer"]
    cfg["peer"] = {
        "bot_id": str(peer.get("bot_id") or "").strip(),
        "name": str(peer.get("name") or "").strip(),
        "qq": str(peer.get("qq") or "").strip(),
        "persona_brief": str(peer.get("persona_brief") or "").strip(),
    }

    try:
        debounce = float(cfg.get("debounce_seconds_open_group", DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS))
    except (TypeError, ValueError):
        debounce = DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS
    cfg["debounce_seconds_open_group"] = max(5.0, min(120.0, debounce))


def read_port(inst_dir: Path) -> int:
    env_path = inst_dir / ".env.qq"
    if not env_path.is_file():
        return int(default_napcat_settings()["port"])
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PORT="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return int(raw)
    return int(default_napcat_settings()["port"])


def write_env_qq(inst_dir: Path, port: int, host: str | None = None) -> None:
    write_env_qq_file(inst_dir, port=port, host=host)


def list_instance_ids() -> list[str]:
    root = instances_dir()
    if not root.is_dir():
        return []
    out: list[str] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "instance.json").is_file():
            out.append(child.name)
    return sorted(out)


def next_free_port() -> int:
    used: set[int] = set()
    for iid in list_instance_ids():
        try:
            used.add(read_port(instances_dir() / iid))
        except (OSError, ValueError):
            continue
    port = int(default_napcat_settings()["port"])
    while port in used:
        port += 1
    return port


def read_instance_files(instance_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_instance_id(instance_id)
    inst_dir = instances_dir() / instance_id
    if not inst_dir.is_dir():
        raise FileNotFoundError(instance_id)
    inst_path = inst_dir / "instance.json"
    per_path = inst_dir / "persona.json"
    if not inst_path.is_file():
        raise FileNotFoundError("instance.json")
    cfg = json.loads(inst_path.read_text(encoding="utf-8"))
    _normalize_instance_config(cfg)
    if per_path.is_file():
        persona = json.loads(per_path.read_text(encoding="utf-8"))
    else:
        persona = _default_persona()
    return cfg, persona


def write_instance_files(
    instance_id: str,
    cfg: dict[str, Any],
    persona: dict[str, Any],
    sync_port: bool = True,
) -> None:
    validate_instance_id(instance_id)
    inst_dir = instances_dir() / instance_id
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "data").mkdir(parents=True, exist_ok=True)
    (inst_dir / "data" / "logs").mkdir(parents=True, exist_ok=True)
    if sync_port and "port" in cfg:
        write_env_qq(inst_dir, int(cfg["port"]))
    _normalize_instance_config(cfg)
    (inst_dir / "instance.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (inst_dir / "persona.json").write_text(
        json.dumps(persona, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def create_instance(
    display_name: str,
    *,
    port: int | None = None,
    qq_mode: str = "napcat",
    soul_slug: str | None = None,
) -> str:
    instance_id = secrets.token_hex(4)
    inst_dir = instances_dir() / instance_id
    if inst_dir.exists():
        raise FileExistsError(instance_id)

    defaults = default_instance_settings()
    use_port = port if port is not None else next_free_port()
    cfg: dict[str, Any] = {
        "id": instance_id,
        "display_name": display_name or defaults["display_name"],
        "port": use_port,
        "qq_mode": qq_mode or defaults["qq_mode"],
        "qq_app_id": defaults["qq_app_id"],
        "qq_app_secret": defaults["qq_app_secret"],
        "owner_ids": default_owner_ids(),
        "open_groups": [],
        "bot_id": instance_id,
        "arbiter_url": defaults.get("arbiter_url") or DEFAULT_ARBITER_URL,
        "arbiter_base_url": defaults.get("arbiter_base_url"),
        "peer": {
            "bot_id": "",
            "name": "",
            "qq": "",
            "persona_brief": "",
        },
        "debounce_seconds_open_group": defaults.get(
            "debounce_seconds_open_group",
            DEFAULT_OPEN_GROUP_DEBOUNCE_SECONDS,
        ),
        "tool_servers": json.loads(json.dumps(DEFAULT_TOOL_SERVERS)),
    }
    persona = _default_persona()

    if soul_slug:
        from . import souls_store

        soul = souls_store.load_soul(soul_slug)
        souls_store.apply_soul_dict_to_instance(cfg, persona, soul)

    instances_dir().mkdir(parents=True, exist_ok=True)
    write_instance_files(instance_id, cfg, persona, sync_port=True)
    return instance_id


def delete_instance(instance_id: str) -> None:
    validate_instance_id(instance_id)
    inst_dir = instances_dir() / instance_id
    if not inst_dir.is_dir():
        raise FileNotFoundError(instance_id)
    trash_root = instances_dir() / "_trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    dest = trash_root / f"{instance_id}_{int(time.time())}"
    shutil.move(str(inst_dir), str(dest))


def merge_update(
    instance_id: str,
    cfg_patch: dict[str, Any] | None,
    persona_patch: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg, persona = read_instance_files(instance_id)
    if cfg_patch:
        for key, value in cfg_patch.items():
            if key == "id":
                continue
            cfg[key] = value
    if persona_patch:
        for key, value in persona_patch.items():
            persona[key] = value
    sync_port = "port" in (cfg_patch or {})
    write_instance_files(instance_id, cfg, persona, sync_port=sync_port)
    return cfg, persona


def instance_dir(instance_id: str) -> Path:
    validate_instance_id(instance_id)
    return instances_dir() / instance_id


def console_log_path(instance_id: str) -> Path:
    return instance_dir(instance_id) / "data" / "logs" / "console.log"


def memory_db_path(instance_id: str) -> Path:
    """Return resolved path to this instance's SQLite memory file (may not exist yet)."""
    validate_instance_id(instance_id)
    return (instance_dir(instance_id) / "data" / "pupu.db").resolve()


def memu_db_path(instance_id: str) -> Path:
    """Return resolved path to this instance's memU SQLite file (may not exist yet)."""
    validate_instance_id(instance_id)
    return (instance_dir(instance_id) / "data" / "memu.db").resolve()


def _is_sqlite_header(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def validate_memory_sqlite_file(path: Path) -> None:
    """Ensure *path* is a readable PuPu-compatible SQLite DB (has ``messages`` table)."""
    if not path.is_file():
        raise ValueError("not a file")
    if path.stat().st_size < 100:
        raise ValueError("file too small to be SQLite")
    if not _is_sqlite_header(path):
        raise ValueError("not a SQLite database (bad header)")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='messages' LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("database missing required table: messages")
    finally:
        conn.close()


def replace_memory_db(instance_id: str, source_path: Path) -> Path:
    """Replace instance ``data/pupu.db`` with *source_path* (validated). Keeps a timestamped ``.bak`` backup if a DB existed."""
    validate_instance_id(instance_id)
    inst_dir = instance_dir(instance_id)
    if not inst_dir.is_dir():
        raise FileNotFoundError(instance_id)
    data_dir = inst_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = (data_dir / "pupu.db").resolve()
    validate_memory_sqlite_file(source_path.resolve())

    ts = int(time.time())
    token = secrets.token_hex(4)
    staging = (data_dir / f"pupu.db.import.{ts}.{token}").resolve()
    backup = (data_dir / f"pupu.db.bak.{ts}.{token}").resolve()

    had_existing = target.is_file()
    if had_existing:
        shutil.copy2(target, backup)

    try:
        shutil.copy2(source_path.resolve(), staging)
        os.replace(staging, target)
    except Exception:
        if had_existing and backup.is_file():
            try:
                shutil.copy2(backup, target)
            except Exception:
                pass
        elif not had_existing and target.is_file():
            try:
                target.unlink()
            except Exception:
                pass
        raise
    finally:
        try:
            if staging.is_file():
                staging.unlink()
        except OSError:
            pass

    return target
