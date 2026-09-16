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
    status_expr = "COALESCE(status, 'active')" if "status" in columns else "'active'"
    superseded_expr = "superseded_by_id" if "superseded_by_id" in columns else "NULL"
    duplicate = conn.execute(
        """
        SELECT 1
        FROM (
            SELECT subject_person_key, object_person_key, scope, fact_key, COUNT(*) AS c
            FROM person_facts
            GROUP BY subject_person_key, object_person_key, scope, fact_key
            HAVING c > 1
        )
        LIMIT 1
        """
    ).fetchone()
    if "legacy_session_id" not in columns and duplicate is None:
        return

    conn.execute("DROP INDEX IF EXISTS idx_person_facts_key")
    conn.execute("DROP INDEX IF EXISTS idx_person_facts_subject")
    conn.execute("DROP INDEX IF EXISTS idx_person_facts_object")
    conn.execute("DROP INDEX IF EXISTS idx_person_facts_legacy_session")
    conn.execute("DROP TABLE IF EXISTS person_facts_new")
    conn.execute(
        """
        CREATE TABLE person_facts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_person_key TEXT NOT NULL,
            object_person_key TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'person',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            superseded_by_id INTEGER,
            source_context_session TEXT NOT NULL DEFAULT '',
            source_msg_start_id INTEGER,
            source_msg_end_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        INSERT OR REPLACE INTO person_facts_new (
            id, subject_person_key, object_person_key, scope, fact_key,
            fact_value, confidence, status, superseded_by_id, source_context_session,
            source_msg_start_id, source_msg_end_id, created_at, updated_at
        )
        SELECT id, subject_person_key, object_person_key, scope, fact_key,
               fact_value, confidence,
               {status_expr}, {superseded_expr}, source_context_session,
               source_msg_start_id, source_msg_end_id, created_at, updated_at
        FROM person_facts pf
        WHERE NOT EXISTS (
            SELECT 1
            FROM person_facts newer
            WHERE newer.subject_person_key = pf.subject_person_key
              AND newer.object_person_key = pf.object_person_key
              AND newer.scope = pf.scope
              AND newer.fact_key = pf.fact_key
              AND (
                  COALESCE(newer.updated_at, '') > COALESCE(pf.updated_at, '')
                  OR (
                      COALESCE(newer.updated_at, '') = COALESCE(pf.updated_at, '')
                      AND newer.id > pf.id
                  )
              )
        )
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
        """
        CREATE TABLE IF NOT EXISTS archived_messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            speaker_key TEXT NOT NULL DEFAULT '',
            speaker_name TEXT NOT NULL DEFAULT '',
            speaker_qq TEXT NOT NULL DEFAULT '',
            archived_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_archived_messages_session
        ON archived_messages(session_id, id)
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
            status TEXT NOT NULL DEFAULT 'active',
            superseded_by_id INTEGER,
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
        CREATE TABLE IF NOT EXISTS memory_mutations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            trigger TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'applied',
            created_at TEXT NOT NULL,
            applied_at TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_mutations_fingerprint
        ON memory_mutations(fingerprint, status, run_date)
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_mutations_entity
        ON memory_mutations(entity_type, entity_id, created_at)
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
            merged_into_thread_id INTEGER,
            compacted_through_step_id INTEGER,
            rollup_summary TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            merge_hint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_thread_aliases (
            session_id TEXT NOT NULL,
            alias_key TEXT NOT NULL,
            canonical_thread_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(session_id, alias_key),
            FOREIGN KEY(canonical_thread_id) REFERENCES event_threads(id)
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_thread_rollups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            through_step_id INTEGER NOT NULL,
            summary TEXT NOT NULL,
            open_loops TEXT NOT NULL DEFAULT '',
            stable_outcomes TEXT NOT NULL DEFAULT '',
            source_step_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(thread_id, through_step_id),
            FOREIGN KEY(thread_id) REFERENCES event_threads(id)
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_thread_rollups_thread
        ON event_thread_rollups(thread_id, through_step_id DESC)
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
        CREATE TABLE IF NOT EXISTS fact_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER NOT NULL,
            source_context_session TEXT NOT NULL DEFAULT '',
            source_msg_start_id INTEGER,
            source_msg_end_id INTEGER,
            evidence_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(fact_id) REFERENCES person_facts(id)
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fact_evidence_fact
        ON fact_evidence(fact_id, source_context_session, source_msg_start_id, source_msg_end_id)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_id TEXT NOT NULL DEFAULT '',
            source_version TEXT NOT NULL DEFAULT '',
            projection_kind TEXT NOT NULL DEFAULT 'rag_card',
            text TEXT NOT NULL,
            embedding BLOB,
            embedding_model TEXT NOT NULL DEFAULT '',
            embedding_dim INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_type, source_key)
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_semantic_cards_source
        ON semantic_cards(source_type, source_key)
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_session TEXT NOT NULL,
            identity_session TEXT NOT NULL,
            start_msg_id INTEGER NOT NULL,
            end_msg_id INTEGER NOT NULL,
            semantic_ids TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_semantic_sync_lookup
        ON semantic_sync_log(identity_session, context_session, start_msg_id, end_msg_id, status)
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
    if "merged_into_thread_id" not in event_thread_columns:
        cursor.execute(
            "ALTER TABLE event_threads ADD COLUMN merged_into_thread_id INTEGER"
        )
    if "compacted_through_step_id" not in event_thread_columns:
        cursor.execute(
            "ALTER TABLE event_threads ADD COLUMN compacted_through_step_id INTEGER"
        )
    if "rollup_summary" not in event_thread_columns:
        cursor.execute(
            "ALTER TABLE event_threads ADD COLUMN rollup_summary TEXT NOT NULL DEFAULT ''"
        )

    _migrate_person_facts_schema(conn)
    person_fact_columns = table_columns(conn, "person_facts")
    if "status" not in person_fact_columns:
        cursor.execute(
            "ALTER TABLE person_facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    if "superseded_by_id" not in person_fact_columns:
        cursor.execute(
            "ALTER TABLE person_facts ADD COLUMN superseded_by_id INTEGER"
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
        CREATE INDEX IF NOT EXISTS idx_person_facts_active
        ON person_facts(status, subject_person_key, object_person_key, scope)
    """
    )
    cursor.execute(
        """INSERT INTO fact_evidence (
               fact_id, source_context_session, source_msg_start_id,
               source_msg_end_id, evidence_text, created_at
           )
           SELECT pf.id,
                  COALESCE(NULLIF(pf.source_context_session, ''), 'legacy'),
                  pf.source_msg_start_id,
                  pf.source_msg_end_id, pf.fact_value, pf.created_at
           FROM person_facts pf
           WHERE NOT EXISTS (
                 SELECT 1 FROM fact_evidence fe
                 WHERE fe.fact_id = pf.id
                   AND fe.source_context_session =
                       COALESCE(NULLIF(pf.source_context_session, ''), 'legacy')
                   AND COALESCE(fe.source_msg_start_id, -1) =
                       COALESCE(pf.source_msg_start_id, -1)
                   AND COALESCE(fe.source_msg_end_id, -1) =
                       COALESCE(pf.source_msg_end_id, -1)
             )"""
    )
    cursor.execute(
        """DELETE FROM fact_evidence
           WHERE fact_id NOT IN (SELECT id FROM person_facts)"""
    )

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
            fact_row = cursor.execute(
                """SELECT id FROM person_facts
                   WHERE subject_person_key = 'instance'
                     AND object_person_key = ''
                     AND scope = 'person' AND fact_key = ?""",
                (key,),
            ).fetchone()
            if fact_row:
                cursor.execute(
                    """INSERT INTO fact_evidence (
                           fact_id, source_context_session, source_msg_start_id,
                           source_msg_end_id, evidence_text, created_at
                       )
                       SELECT ?, 'persona_seed', NULL, NULL, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM fact_evidence
                           WHERE fact_id = ? AND source_context_session = 'persona_seed'
                             AND evidence_text = ?
                       )""",
                    (int(fact_row["id"]), value, now, int(fact_row["id"]), value),
                )

    conn.commit()
    conn.close()
