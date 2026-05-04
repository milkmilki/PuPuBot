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


def read_port(inst_dir: Path) -> int:
    env_path = inst_dir / ".env.qq"
    if not env_path.is_file():
        return 8081
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PORT="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return int(raw)
    return 8081


def write_env_qq(inst_dir: Path, port: int, host: str = "0.0.0.0") -> None:
    text = (
        f"HOST={host}\n"
        f"PORT={port}\n"
        'COMMAND_START=["/"]\n'
        'COMMAND_SEP=["."]\n'
    )
    (inst_dir / ".env.qq").write_text(text, encoding="utf-8")


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
    port = 8081
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
    _scrub_deprecated_instance_keys(cfg)
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

    use_port = port if port is not None else next_free_port()
    cfg: dict[str, Any] = {
        "id": instance_id,
        "display_name": display_name or "仆仆",
        "port": use_port,
        "qq_mode": qq_mode,
        "qq_app_id": "",
        "qq_app_secret": "",
        "owner_ids": [],
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
