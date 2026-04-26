"""SQLite-based storage for messages, familiarity, summaries, facts, and reminders."""

import sqlite3
import os
import calendar
from datetime import datetime

from .familiarity import score_to_level

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pupu.db")


def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    from .persona import SEED_SELF_FACTS

    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS familiarity (
            session_id TEXT PRIMARY KEY,
            score INTEGER NOT NULL DEFAULT 0,
            level TEXT NOT NULL DEFAULT '认识',
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            date TEXT NOT NULL,
            delta INTEGER NOT NULL,
            description TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            summary TEXT NOT NULL,
            start_msg_id INTEGER NOT NULL,
            end_msg_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS self_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_facts_session
        ON user_facts(session_id)
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_facts_key
        ON user_facts(session_id, fact_key)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_self_facts_session
        ON self_facts(session_id)
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_self_facts_key
        ON self_facts(session_id, fact_key)
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL,
            run_at TEXT NOT NULL,
            repeat_kind TEXT NOT NULL DEFAULT 'once',
            interval_seconds INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_scheduled_session
        ON scheduled_tasks(session_id, enabled, run_at)
    """)
    # Seed initial self_facts for owner session (only fills missing keys)
    if SEED_SELF_FACTS:
        now = datetime.now().isoformat()
        for key, value in SEED_SELF_FACTS.items():
            c.execute(
                "INSERT OR IGNORE INTO self_facts (session_id, fact_key, fact_value, updated_at) VALUES (?, ?, ?, ?)",
                ("owner", key, value, now),
            )
    conn.commit()
    conn.close()


def _ensure_familiarity(conn, session_id: str):
    row = conn.execute(
        "SELECT session_id FROM familiarity WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO familiarity (session_id, score, level, updated_at) VALUES (?, 0, '认识', ?)",
            (session_id, datetime.now().isoformat()),
        )
        conn.commit()


def save_message(role: str, content: str, session_id: str = "default"):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, role, content, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_recent_messages(n: int = 50, session_id: str = "default") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, n),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def get_familiarity(session_id: str = "default") -> int:
    conn = _get_conn()
    _ensure_familiarity(conn, session_id)
    row = conn.execute(
        "SELECT score FROM familiarity WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return row["score"] if row else 0


def update_familiarity(delta: int, reason: str, session_id: str = "default"):
    conn = _get_conn()
    _ensure_familiarity(conn, session_id)
    row = conn.execute(
        "SELECT score FROM familiarity WHERE session_id = ?", (session_id,)
    ).fetchone()
    old_score = row["score"] if row else 0
    new_score = max(0, min(100, old_score + delta))
    new_level = score_to_level(new_score)
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE familiarity SET score = ?, level = ?, updated_at = ? WHERE session_id = ?",
        (new_score, new_level, now, session_id),
    )
    conn.execute(
        "INSERT INTO events (session_id, date, delta, description) VALUES (?, ?, ?, ?)",
        (session_id, now, delta, reason),
    )
    conn.commit()
    conn.close()


def get_event_log(limit: int = 20, session_id: str = "default") -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date, delta, description FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [
        {"date": r["date"], "delta": r["delta"], "description": r["description"]}
        for r in reversed(rows)
    ]


def get_oldest_unsummarized_msg_id(session_id: str = "default") -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT MAX(end_msg_id) as last_end FROM summaries WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    last_end = row["last_end"] if row and row["last_end"] else 0
    conn.close()
    return last_end


def get_messages_in_range(
    session_id: str, after_id: int, limit: int = 100
) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (session_id, after_id, limit),
    ).fetchall()
    conn.close()
    return [
        {"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows
    ]


def count_messages(session_id: str = "default") -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?", (session_id,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def save_summary(
    summary: str, start_msg_id: int, end_msg_id: int, session_id: str = "default"
):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO summaries (session_id, summary, start_msg_id, end_msg_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, summary, start_msg_id, end_msg_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_summaries(session_id: str = "default", limit: int = 5) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT summary, created_at FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"summary": r["summary"], "created_at": r["created_at"]} for r in reversed(rows)]


def upsert_user_facts(facts: dict[str, str], session_id: str = "default"):
    conn = _get_conn()
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
    conn = _get_conn()
    rows = conn.execute(
        "SELECT fact_key, fact_value FROM user_facts WHERE session_id = ? ORDER BY updated_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return {r["fact_key"]: r["fact_value"] for r in rows}


def upsert_self_facts(facts: dict[str, str], session_id: str = "default"):
    conn = _get_conn()
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
    conn = _get_conn()
    rows = conn.execute(
        "SELECT fact_key, fact_value FROM self_facts WHERE session_id = ? ORDER BY updated_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return {r["fact_key"]: r["fact_value"] for r in rows}


def reset_session(session_id: str):
    conn = _get_conn()
    for table in (
        "messages",
        "familiarity",
        "events",
        "user_facts",
        "summaries",
        "self_facts",
        "scheduled_tasks",
    ):
        conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()


def get_last_user_message_time(session_id: str = "default") -> str | None:
    """Return ISO timestamp of the most recent user message, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT timestamp FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["timestamp"] if row else None


def get_last_message_time(session_id: str = "default") -> str | None:
    """Return ISO timestamp of the most recent message (user or assistant), or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT timestamp FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["timestamp"] if row else None


def get_familiarity_info(session_id: str = "default") -> dict:
    conn = _get_conn()
    _ensure_familiarity(conn, session_id)
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


MAX_SCHEDULED_TASKS_PER_SESSION = 30


def count_scheduled_tasks(session_id: str) -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM scheduled_tasks WHERE session_id = ? AND enabled = 1",
        (session_id,),
    ).fetchone()
    conn.close()
    return int(row["c"]) if row else 0


def create_scheduled_task(
    session_id: str,
    title: str,
    instruction: str,
    run_at: str,
    repeat_kind: str,
    interval_seconds: int | None,
) -> int:
    conn = _get_conn()
    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO scheduled_tasks
           (session_id, title, instruction, run_at, repeat_kind, interval_seconds, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            session_id,
            title or "提醒",
            instruction,
            run_at,
            repeat_kind,
            interval_seconds,
            now,
        ),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return int(tid)


def list_scheduled_tasks(session_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, title, instruction, run_at, repeat_kind, interval_seconds, created_at
           FROM scheduled_tasks
           WHERE session_id = ? AND enabled = 1
           ORDER BY run_at ASC""",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_scheduled_task(session_id: str, task_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE scheduled_tasks SET enabled = 0 WHERE id = ? AND session_id = ? AND enabled = 1",
        (task_id, session_id),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n > 0


def get_due_scheduled_tasks(before_iso: str, limit: int = 10) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, session_id, title, instruction, run_at, repeat_kind, interval_seconds
           FROM scheduled_tasks
           WHERE enabled = 1 AND run_at <= ?
           ORDER BY run_at ASC
           LIMIT ?""",
        (before_iso, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def finalize_scheduled_task(
    task_id: int,
    old_run_at: str,
    repeat_kind: str,
    interval_seconds: int | None,
) -> bool:
    """After a successful fire: delete if once, else bump run_at. Returns False if row was already changed."""
    fired = datetime.now()
    rk = (repeat_kind or "once").lower()
    next_at = None
    if rk != "once":
        next_at = _compute_next_run_at_iso(fired, rk, interval_seconds)
    conn = _get_conn()
    if next_at is None:
        cur = conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = ? AND run_at = ?",
            (task_id, old_run_at),
        )
    else:
        cur = conn.execute(
            "UPDATE scheduled_tasks SET run_at = ? WHERE id = ? AND run_at = ?",
            (next_at, task_id, old_run_at),
        )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def _compute_next_run_at_iso(
    fired_at: datetime, repeat_kind: str, interval_seconds: int | None
) -> str | None:
    from datetime import timedelta

    def _add_months(dt: datetime, months: int) -> datetime:
        total_month = (dt.year * 12 + (dt.month - 1)) + months
        year = total_month // 12
        month = total_month % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(dt.day, last_day)
        return dt.replace(year=year, month=month, day=day)

    rk = (repeat_kind or "once").lower()
    if rk == "once":
        return None
    if rk == "daily":
        n = fired_at + timedelta(days=1)
    elif rk == "weekly":
        n = fired_at + timedelta(weeks=1)
    elif rk == "monthly":
        n = _add_months(fired_at, 1)
    elif rk == "yearly":
        n = _add_months(fired_at, 12)
    elif rk == "interval":
        sec = int(interval_seconds) if interval_seconds else 3600
        sec = max(60, min(sec, 86400 * 7))
        n = fired_at + timedelta(seconds=sec)
    else:
        return None
    return n.isoformat(timespec="seconds")
