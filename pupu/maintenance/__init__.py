"""Daily maintenance public API."""

from datetime import datetime

from ..storage.db import get_conn, init_db
from .constants import BUSY_REPORT_PREFIX, MAINTENANCE_HOUR
from .history import _has_successful_auto_run
from .runner import run_memory_maintenance as _run_memory_maintenance


def run_memory_maintenance(
    trigger: str = "manual",
    include_model: bool = True,
    now: datetime | None = None,
) -> str:
    return _run_memory_maintenance(trigger=trigger, include_model=include_model, now=now)


def maybe_run_daily_maintenance(now: datetime | None = None) -> str | None:
    current = now or datetime.now()
    if current.hour < MAINTENANCE_HOUR:
        return None

    init_db()
    conn = get_conn()
    try:
        if _has_successful_auto_run(conn, current.date().isoformat()):
            return None
    finally:
        conn.close()

    report = run_memory_maintenance(trigger="auto", include_model=True, now=current)
    if report.startswith(BUSY_REPORT_PREFIX):
        return None
    return report


__all__ = [
    "MAINTENANCE_HOUR",
    "maybe_run_daily_maintenance",
    "run_memory_maintenance",
]
