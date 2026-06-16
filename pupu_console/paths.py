"""Repository root resolution for the multi-instance console."""

from __future__ import annotations

import os
from pathlib import Path


def get_repo_root() -> Path:
    env = os.environ.get("PUPU_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def instances_dir() -> Path:
    return get_repo_root() / "instances"


def souls_dir() -> Path:
    return get_repo_root() / "souls"
