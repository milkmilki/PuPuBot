"""Soul presets: reusable persona + soul-level config under ``souls/``."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import instance_store
from .paths import souls_dir

_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_SOUL_INSTANCE_KEYS = frozenset({"tool_servers"})
_SOUL_PERSONA_KEYS = frozenset({"name", "core_persona", "seed_self_facts"})
_DEPRECATED_SOUL_KEYS = frozenset({"mode", "persona_enabled", "llm"})


def validate_slug(slug: str) -> None:
    if not slug or not _SLUG_RE.match(slug):
        raise ValueError("invalid soul slug")


def soul_path(slug: str) -> Path:
    validate_slug(slug)
    return souls_dir() / f"{slug}.json"


def list_soul_slugs() -> list[str]:
    root = souls_dir()
    if not root.is_dir():
        return []
    out: list[str] = []
    for child in root.glob("*.json"):
        if child.parent.name == "_trash":
            continue
        out.append(child.stem)
    return sorted(out)


def load_soul(slug: str) -> dict[str, Any]:
    path = soul_path(slug)
    if not path.is_file():
        raise FileNotFoundError(slug)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("soul file must be a JSON object")
    return data


def save_soul(slug: str, soul: dict[str, Any]) -> None:
    validate_slug(slug)
    souls_dir().mkdir(parents=True, exist_ok=True)
    body = dict(soul)
    for k in _DEPRECATED_SOUL_KEYS:
        body.pop(k, None)
    body["slug"] = slug
    soul_path(slug).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_soul(slug: str) -> None:
    validate_slug(slug)
    path = soul_path(slug)
    if not path.is_file():
        raise FileNotFoundError(slug)
    trash = souls_dir() / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{slug}_{int(time.time())}.json"
    shutil.move(str(path), str(dest))


def apply_soul_dict_to_instance(
    cfg: dict[str, Any],
    persona: dict[str, Any],
    soul: dict[str, Any],
) -> None:
    for key in _SOUL_INSTANCE_KEYS:
        if key in soul:
            cfg[key] = json.loads(json.dumps(soul[key]))
    for key in _SOUL_PERSONA_KEYS:
        if key not in soul:
            continue
        if key == "seed_self_facts" and isinstance(soul[key], dict):
            persona[key] = {str(k): str(v) for k, v in soul[key].items()}
        else:
            persona[key] = soul[key]


def apply_to_instance(slug: str, instance_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    soul = load_soul(slug)
    cfg, persona = instance_store.read_instance_files(instance_id)
    apply_soul_dict_to_instance(cfg, persona, soul)
    instance_store.write_instance_files(instance_id, cfg, persona, sync_port=False)
    return cfg, persona


def capture_from_instance(instance_id: str, slug: str, display_name: str) -> dict[str, Any]:
    validate_slug(slug)
    cfg, persona = instance_store.read_instance_files(instance_id)
    soul: dict[str, Any] = {
        "slug": slug,
        "display_name": display_name,
        "name": persona.get("name", "仆仆"),
        "core_persona": persona.get("core_persona", ""),
        "seed_self_facts": persona.get("seed_self_facts", {}),
        "tool_servers": cfg.get("tool_servers", {}),
    }
    save_soul(slug, soul)
    return soul
