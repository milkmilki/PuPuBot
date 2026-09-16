"""Bounded diagnostics for malformed maintenance-model responses."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from ..logging_utils import get_runtime_log_directory

ERROR_LOG_PREFIX = "maintenance-model-errors-"
ERROR_LOG_RETENTION_DAYS = 14
RAW_RESPONSE_MAX_CHARS = 1_000_000

_error_log_lock = threading.Lock()


def record_model_response_error(
    *,
    task_name: str,
    provider: str,
    raw_response: object,
    error: Exception,
    now: datetime | None = None,
    log_dir: Path | None = None,
) -> Path:
    """Append one parse failure without allowing diagnostics to grow unbounded."""

    timestamp = now or datetime.now()
    raw_text = "" if raw_response is None else str(raw_response)
    raw_bytes = raw_text.encode("utf-8", errors="replace")
    retained_text = raw_text[:RAW_RESPONSE_MAX_CHARS]
    record = {
        "timestamp": timestamp.isoformat(),
        "task_name": str(task_name or "maintenance"),
        "provider": str(provider or "unknown"),
        "error_type": type(error).__name__,
        "error": str(error),
        "raw_response": retained_text,
        "raw_response_chars": len(raw_text),
        "raw_response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "truncated": len(retained_text) < len(raw_text),
    }
    directory = Path(log_dir) if log_dir is not None else get_runtime_log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ERROR_LOG_PREFIX}{timestamp:%Y%m%d}.jsonl"
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

    with _error_log_lock:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as sink:
            sink.write(line)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _prune_error_logs(directory)
    return path


def _prune_error_logs(log_dir: Path) -> list[Path]:
    logs = sorted(
        (
            path
            for path in log_dir.glob(f"{ERROR_LOG_PREFIX}*.jsonl")
            if _is_daily_error_log(path)
        ),
        reverse=True,
    )
    deleted: list[Path] = []
    for path in logs[ERROR_LOG_RETENTION_DAYS:]:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(path)
    return deleted


def _is_daily_error_log(path: Path) -> bool:
    name = path.name
    date_text = name[len(ERROR_LOG_PREFIX) : -len(".jsonl")]
    return len(date_text) == 8 and date_text.isdigit()
