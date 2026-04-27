"""Persistence helpers for familiarity score and legacy event history."""

from __future__ import annotations

from datetime import datetime

from ..familiarity import score_to_level
from .db import get_conn


def ensure_familiarity(conn, session_id: str):
    row = conn.execute(
        "SELECT session_id FROM familiarity WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO familiarity (session_id, score, level, updated_at) VALUES (?, 0, '认识', ?)",
            (session_id, datetime.now().isoformat()),
        )
        conn.commit()


def get_familiarity(session_id: str = "default") -> int:
    conn = get_conn()
    ensure_familiarity(conn, session_id)
    row = conn.execute(
        "SELECT score FROM familiarity WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["score"] if row else 0


def update_familiarity(
    delta: int,
    reason: str | None = None,
    session_id: str = "default",
    record_event: bool = False,
):
    conn = get_conn()
    ensure_familiarity(conn, session_id)
    row = conn.execute(
        "SELECT score FROM familiarity WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    old_score = row["score"] if row else 0
    new_score = max(0, min(100, old_score + int(delta)))
    new_level = score_to_level(new_score)
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE familiarity SET score = ?, level = ?, updated_at = ? WHERE session_id = ?",
        (new_score, new_level, now, session_id),
    )
    if record_event and reason:
        conn.execute(
            "INSERT INTO events (session_id, date, delta, description) VALUES (?, ?, ?, ?)",
            (session_id, now, int(delta), str(reason).strip()),
        )
    conn.commit()
    conn.close()


def set_familiarity(
    score: int,
    session_id: str = "default",
    reason: str | None = None,
    write_event: bool = False,
):
    conn = get_conn()
    ensure_familiarity(conn, session_id)
    new_score = max(0, min(100, int(score)))
    new_level = score_to_level(new_score)
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE familiarity SET score = ?, level = ?, updated_at = ? WHERE session_id = ?",
        (new_score, new_level, now, session_id),
    )
    if write_event and reason:
        conn.execute(
            "INSERT INTO events (session_id, date, delta, description) VALUES (?, ?, ?, ?)",
            (session_id, now, 0, reason),
        )
    conn.commit()
    conn.close()


def get_event_log(limit: int = 20, session_id: str = "default") -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, delta, description FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [
        {"date": row["date"], "delta": row["delta"], "description": row["description"]}
        for row in reversed(rows)
    ]


def get_familiarity_info(session_id: str = "default") -> dict:
    conn = get_conn()
    ensure_familiarity(conn, session_id)
    row = conn.execute(
        "SELECT score, level, updated_at FROM familiarity WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    if row:
        return {
            "score": row["score"],
            "level": row["level"],
            "updated_at": row["updated_at"],
        }
    return {"score": 0, "level": "认识", "updated_at": ""}
