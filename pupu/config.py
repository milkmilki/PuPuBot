"""Shared config loading helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# Used when config omits ``owner_ids`` and for new console instances.
DEFAULT_OWNER_IDS: list[str] = ["424225912"]


def get_config_path() -> Path:
    override = os.environ.get("PUPU_CONFIG_PATH")
    if override:
        return Path(override)
    return CONFIG_PATH


def load_config() -> dict:
    path = get_config_path()
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def save_config(config: dict) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def load_owner_ids() -> list[str]:
    try:
        config = load_config()
    except Exception:
        return list(DEFAULT_OWNER_IDS)
    return [str(value) for value in config.get("owner_ids", DEFAULT_OWNER_IDS)]


def load_owner_id_set() -> set[str]:
    return set(load_owner_ids())


def load_first_numeric_owner_id() -> int | None:
    for owner_id in load_owner_ids():
        if owner_id.isdigit():
            return int(owner_id)
    return None
