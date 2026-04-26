"""Persistence helpers for user and self facts."""

from __future__ import annotations

from datetime import datetime

from .db import get_conn


def upsert_user_facts(facts: dict[str, str], session_id: str = "default"):
    conn = get_conn()
    now = datetime.now().isoformat()
    for key, value in facts.items():
        if not value or value.strip() == "":
            conn.execute(
                "DELETE FROM user_facts WHERE session_id = ? AND fact_key = ?",
                (session_id, key),
            )
        else:
            conn.execute(
                """INSERT INTO user_facts (session_id, fact_key, fact_value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id, fact_key) DO UPDATE SET fact_value = ?, updated_at = ?""",
                (session_id, key, value, now, value, now),
            )
    conn.commit()
    conn.close()


def get_user_facts(session_id: str = "default") -> dict[str, str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT fact_key, fact_value FROM user_facts WHERE session_id = ? ORDER BY updated_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return {row["fact_key"]: row["fact_value"] for row in rows}


def upsert_self_facts(facts: dict[str, str], session_id: str = "default"):
    conn = get_conn()
    now = datetime.now().isoformat()
    for key, value in facts.items():
        if not value or value.strip() == "":
            conn.execute(
                "DELETE FROM self_facts WHERE session_id = ? AND fact_key = ?",
                (session_id, key),
            )
        else:
            conn.execute(
                """INSERT INTO self_facts (session_id, fact_key, fact_value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id, fact_key) DO UPDATE SET fact_value = ?, updated_at = ?""",
                (session_id, key, value, now, value, now),
            )
    conn.commit()
    conn.close()


def get_self_facts(session_id: str = "default") -> dict[str, str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT fact_key, fact_value FROM self_facts WHERE session_id = ? ORDER BY updated_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return {row["fact_key"]: row["fact_value"] for row in rows}
