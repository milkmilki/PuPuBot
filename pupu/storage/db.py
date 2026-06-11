"""Database path resolution, connections, and schema initialization."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from ..sessions import OWNER_SESSION

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "pupu.db",
)


def get_db_path() -> str:
    return os.environ.get("PUPU_DB_PATH", DEFAULT_DB_PATH)


def get_data_dir() -> str:
    return os.path.dirname(get_db_path())


def get_conn():
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def init_db():
    from ..familiarity import DEFAULT_FAMILIARITY_LEVEL, DEFAULT_FAMILIARITY_SCORE
    from ..persona.core import get_seed_self_facts

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'chat'
        )
    """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS familiarity (
            session_id TEXT PRIMARY KEY,
            score INTEGER NOT NULL DEFAULT {DEFAULT_FAMILIARITY_SCORE},
            level TEXT NOT NULL DEFAULT '{DEFAULT_FAMILIARITY_LEVEL}',
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            date TEXT NOT NULL,
            delta INTEGER NOT NULL,
            description TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            summary TEXT NOT NULL,
            start_msg_id INTEGER NOT NULL,
            end_msg_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS self_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_facts_session
        ON user_facts(session_id)
    """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_facts_key
        ON user_facts(session_id, fact_key)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_self_facts_session
        ON self_facts(session_id)
    """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_self_facts_key
        ON self_facts(session_id, fact_key)
    """
    )
    cursor.execute(
        """
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
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scheduled_session
        ON scheduled_tasks(session_id, enabled, run_at)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            report TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_maintenance_runs_lookup
        ON maintenance_runs(run_date, trigger, status)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS important_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            source_event_key TEXT NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            event_time TEXT,
            time_text TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            followup_hint TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            linked_task_id INTEGER,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_important_events_key
        ON important_events(session_id, source_event_key)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_important_events_prompt
        ON important_events(session_id, status, event_time, last_seen_at)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            key TEXT NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            current_step_id INTEGER,
            event_time TEXT,
            time_text TEXT NOT NULL DEFAULT '',
            followup_hint TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            linked_task_id INTEGER,
            search_text TEXT NOT NULL DEFAULT '',
            merge_hint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_event_threads_key
        ON event_threads(session_id, key)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_threads_prompt
        ON event_threads(session_id, status, event_time, updated_at)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            step_type TEXT NOT NULL DEFAULT 'user',
            summary TEXT NOT NULL,
            cause TEXT NOT NULL DEFAULT '',
            reflection TEXT NOT NULL DEFAULT '',
            occurred_at TEXT,
            source_msg_start_id INTEGER,
            source_msg_end_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(thread_id) REFERENCES event_threads(id)
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_steps_thread
        ON event_steps(thread_id, created_at, id)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memu_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_session TEXT NOT NULL,
            identity_session TEXT NOT NULL,
            start_msg_id INTEGER NOT NULL,
            end_msg_id INTEGER NOT NULL,
            memu_ids TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memu_sync_lookup
        ON memu_sync_log(identity_session, context_session, start_msg_id, end_msg_id, status)
    """
    )

    message_columns = table_columns(conn, "messages")
    if "source" not in message_columns:
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'"
        )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_review
        ON messages(session_id, source, role, id)
    """
    )

    seed = get_seed_self_facts()
    if seed:
        now = datetime.now().isoformat()
        for key, value in seed.items():
            cursor.execute(
                "INSERT OR IGNORE INTO self_facts (session_id, fact_key, fact_value, updated_at) VALUES (?, ?, ?, ?)",
                (OWNER_SESSION, key, value, now),
            )

    conn.commit()
    conn.close()
