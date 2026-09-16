"""Daily SQLite backup helpers."""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .storage.db import get_db_path, init_db

BACKUP_HOUR = 3
BACKUP_RETENTION_DAYS = 3


def get_backup_dir() -> Path:
    env_path = os.environ.get("PUPU_BACKUP_DIR")
    if env_path:
        backup_dir = Path(env_path)
    else:
        backup_dir = Path(get_db_path()).resolve().parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def get_backup_path(for_date) -> Path:
    date_text = for_date.strftime("%Y%m%d")
    return get_backup_dir() / f"pupu-{date_text}.db"


def _backup_sort_key(path: Path) -> tuple[str, str]:
    name = path.name
    date_text = name[5:-3] if name.startswith("pupu-") and name.endswith(".db") else ""
    if len(date_text) == 8 and date_text.isdigit():
        return name[5:-3], name
    return "", name


def _daily_backup_paths() -> list[Path]:
    out = []
    for path in get_backup_dir().glob("pupu-*.db"):
        name = path.name
        date_text = name[5:-3] if name.startswith("pupu-") and name.endswith(".db") else ""
        if len(date_text) == 8 and date_text.isdigit():
            out.append(path)
    return out


def prune_old_backups(keep: int = BACKUP_RETENTION_DAYS) -> list[Path]:
    """Delete older PuPu daily backup snapshots, keeping the newest files."""
    try:
        keep_count = max(1, int(keep))
    except Exception:
        keep_count = BACKUP_RETENTION_DAYS

    backups = sorted(_daily_backup_paths(), key=_backup_sort_key, reverse=True)
    deleted: list[Path] = []
    for path in backups[keep_count:]:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(path)
    return deleted


def run_database_backup(now: datetime | None = None, overwrite: bool = False) -> str:
    current = now or datetime.now()
    init_db()

    source_path = Path(get_db_path()).resolve()
    backup_path = get_backup_path(current.date())
    if backup_path.exists() and not overwrite:
        deleted = prune_old_backups()
        note = f"，清理旧备份 {len(deleted)} 个" if deleted else ""
        return f"数据库备份已存在：{backup_path}{note}"

    temp_path = backup_path.with_suffix(".tmp")
    if temp_path.exists():
        temp_path.unlink()

    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(temp_path), timeout=30)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()

    temp_path.replace(backup_path)
    deleted = prune_old_backups()
    note = f"，清理旧备份 {len(deleted)} 个" if deleted else ""
    return f"数据库备份完成：{backup_path}{note}"


def maybe_run_daily_backup(now: datetime | None = None) -> str | None:
    current = now or datetime.now()
    if current.hour < BACKUP_HOUR:
        return None

    backup_path = get_backup_path(current.date())
    if backup_path.exists():
        prune_old_backups()
        return None

    return run_database_backup(now=current, overwrite=False)
