"""Shared config loading helpers."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def get_config_path() -> Path:
    return CONFIG_PATH


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as file:
        return json.load(file)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def load_owner_ids() -> list[str]:
    try:
        config = load_config()
    except Exception:
        return []
    return [str(value) for value in config.get("owner_ids", [])]


def load_owner_id_set() -> set[str]:
    return set(load_owner_ids())


def load_first_numeric_owner_id() -> int | None:
    for owner_id in load_owner_ids():
        if owner_id.isdigit():
            return int(owner_id)
    return None
