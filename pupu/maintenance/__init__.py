"""Daily maintenance public API."""

from datetime import datetime

from ..storage.db import get_conn, init_db
from ..sessions import OWNER_SESSION
from .constants import BUSY_REPORT_PREFIX, MAINTENANCE_HOUR
from .history import _has_successful_auto_run, _record_maintenance_run
from .runner import run_memory_maintenance as _run_memory_maintenance


def run_memu_tidy(*args, **kwargs):
    from ..memory_index import run_memu_tidy as _run_memu_tidy

    return _run_memu_tidy(*args, **kwargs)


def format_memu_tidy_report(*args, **kwargs):
    from ..memory_index import format_memu_tidy_report as _format_memu_tidy_report

    return _format_memu_tidy_report(*args, **kwargs)


def run_memory_maintenance(
    trigger: str = "manual",
    include_model: bool = True,
    now: datetime | None = None,
    memu_mode: str = "apply",
) -> str:
    return _run_memory_maintenance(
        trigger=trigger,
        include_model=include_model,
        now=now,
        memu_mode=memu_mode,
    )


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

    report = run_memory_maintenance(
        trigger="auto",
        include_model=True,
        now=current,
        memu_mode="apply",
    )
    if report.startswith(BUSY_REPORT_PREFIX):
        return None
    return report


def maybe_run_daily_memu_tidy(now: datetime | None = None) -> str | None:
    current = now or datetime.now()
    if current.hour != MAINTENANCE_HOUR:
        return None

    run_date = current.date().isoformat()
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT 1
               FROM maintenance_runs
               WHERE run_date = ? AND trigger = 'auto_memu_tidy'
               LIMIT 1""",
            (run_date,),
        ).fetchone()
        if row:
            return None
    finally:
        conn.close()

    status = "success"
    report = ""
    try:
        result = run_memu_tidy(OWNER_SESSION, mode="apply", now=current)
        if result.get("status") == "busy":
            return None
        report = format_memu_tidy_report(
            result,
            identity_session=OWNER_SESSION,
            mode="apply",
            trigger="auto",
        )
    except Exception as exc:
        status = "failed"
        report = f"memU tidy auto run failed: {exc}"

    conn = get_conn()
    try:
        _record_maintenance_run(conn, run_date, "auto_memu_tidy", status, report)
        conn.commit()
    finally:
        conn.close()
    return report


__all__ = [
    "MAINTENANCE_HOUR",
    "maybe_run_daily_maintenance",
    "maybe_run_daily_memu_tidy",
    "run_memory_maintenance",
]
