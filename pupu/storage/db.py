"""Database path resolution, connections, and schema initialization."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..instance_context import require_current_instance_context


def get_db_path() -> str:
    return str(require_current_instance_context().db_path)


def get_data_dir() -> str:
    return str(require_current_instance_context().data_dir)


def get_conn():
    db_path = get_db_path()
    from pathlib import Path

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _migrate_person_facts_schema(conn) -> None:
    columns = table_columns(conn, "person_facts")
    if "legacy_session_id" not in columns:
        conn.execute("DROP INDEX IF EXISTS idx_person_facts_legacy_session")
        return

    conn.execute("DROP INDEX IF EXISTS idx_person_facts_key")
    conn.execute("DROP INDEX IF EXISTS idx_person_facts_subject")
    conn.execute("DROP INDEX IF EXISTS idx_person_facts_object")
    conn.execute("DROP INDEX IF EXISTS idx_person_facts_legacy_session")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS person_facts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_person_key TEXT NOT NULL,
            object_person_key TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'person',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            source_context_session TEXT NOT NULL DEFAULT '',
            source_msg_start_id INTEGER,
            source_msg_end_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO person_facts_new (
            id, subject_person_key, object_person_key, scope, fact_key,
            fact_value, confidence, source_context_session,
            source_msg_start_id, source_msg_end_id, created_at, updated_at
        )
        SELECT id, subject_person_key, object_person_key, scope, fact_key,
               fact_value, confidence, source_context_session,
               source_msg_start_id, source_msg_end_id, created_at, updated_at
        FROM person_facts
        """
    )
    conn.execute("DROP TABLE person_facts")
    conn.execute("ALTER TABLE person_facts_new RENAME TO person_facts")


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
            source TEXT NOT NULL DEFAULT 'chat',
            speaker_key TEXT NOT NULL DEFAULT '',
            speaker_name TEXT NOT NULL DEFAULT '',
            speaker_qq TEXT NOT NULL DEFAULT ''
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
        CREATE TABLE IF NOT EXISTS person_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_person_key TEXT NOT NULL,
            object_person_key TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'person',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            source_context_session TEXT NOT NULL DEFAULT '',
            source_msg_start_id INTEGER,
            source_msg_end_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_person_facts_key
        ON person_facts(subject_person_key, object_person_key, scope, fact_key)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_person_facts_subject
        ON person_facts(subject_person_key, updated_at)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_person_facts_object
        ON person_facts(object_person_key, updated_at)
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
        CREATE TABLE IF NOT EXISTS event_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            key TEXT NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            current_step_id INTEGER,
            origin_person_key TEXT NOT NULL DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS people (
            person_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            qq_id TEXT NOT NULL DEFAULT '',
            aliases TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            step_id INTEGER,
            person_key TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'participant',
            source TEXT NOT NULL DEFAULT 'inferred',
            created_at TEXT NOT NULL,
            UNIQUE(thread_id, step_id, person_key, role),
            FOREIGN KEY(thread_id) REFERENCES event_threads(id)
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_people_thread
        ON event_people(thread_id, step_id, person_key)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_people_person
        ON event_people(person_key, thread_id)
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
    if "speaker_key" not in message_columns:
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN speaker_key TEXT NOT NULL DEFAULT ''"
        )
    if "speaker_name" not in message_columns:
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN speaker_name TEXT NOT NULL DEFAULT ''"
        )
    if "speaker_qq" not in message_columns:
        cursor.execute(
            "ALTER TABLE messages ADD COLUMN speaker_qq TEXT NOT NULL DEFAULT ''"
        )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_review
        ON messages(session_id, source, role, id)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_speaker_range
        ON messages(session_id, id, speaker_key)
    """
    )

    event_thread_columns = table_columns(conn, "event_threads")
    if "origin_person_key" not in event_thread_columns:
        cursor.execute(
            "ALTER TABLE event_threads ADD COLUMN origin_person_key TEXT NOT NULL DEFAULT ''"
        )

    _migrate_person_facts_schema(conn)

    for old_table in ("important_events", "user_facts", "self_facts"):
        cursor.execute(f"DROP TABLE IF EXISTS {old_table}")

    try:
        from .event_threads import ensure_event_thread_fts, rebuild_event_thread_fts
        from .people import backfill_default_event_people

        backfill_default_event_people(conn)
        if ensure_event_thread_fts(conn):
            rebuild_event_thread_fts(conn)
    except Exception:
        # FTS5 and event-person backfill are optional accelerators. Core storage
        # still works if this environment cannot initialize them during startup.
        pass

    seed = get_seed_self_facts()
    if seed:
        now = datetime.now().isoformat()
        for key, value in seed.items():
            cursor.execute(
                """INSERT OR IGNORE INTO person_facts (
                       subject_person_key, object_person_key, scope,
                       fact_key, fact_value, confidence, source_context_session,
                       source_msg_start_id, source_msg_end_id, created_at, updated_at
                   ) VALUES ('instance', '', 'person', ?, ?, 1.0, '', NULL, NULL, ?, ?)""",
                (key, value, now, now),
            )

    conn.commit()
    conn.close()
