"""Persistence helpers for memU synchronization attempts."""

from __future__ import annotations

import json
from datetime import datetime

from .db import get_conn


def has_successful_memu_sync(
    *,
    context_session: str,
    identity_session: str,
    start_msg_id: int,
    end_msg_id: int,
) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM memu_sync_log
            WHERE context_session = ?
              AND identity_session = ?
              AND start_msg_id = ?
              AND end_msg_id = ?
              AND status = 'success'
            LIMIT 1
            """,
            (context_session, identity_session, int(start_msg_id), int(end_msg_id)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_memu_sync(
    *,
    context_session: str,
    identity_session: str,
    start_msg_id: int,
    end_msg_id: int,
    memu_ids: list[str] | None = None,
    status: str,
    error: str = "",
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO memu_sync_log (
                context_session,
                identity_session,
                start_msg_id,
                end_msg_id,
                memu_ids,
                status,
                error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context_session,
                identity_session,
                int(start_msg_id),
                int(end_msg_id),
                json.dumps(memu_ids or [], ensure_ascii=False),
                status,
                error[:2000],
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
