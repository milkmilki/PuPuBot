"""Old-message pruning helpers."""

from datetime import datetime, timedelta

from .constants import KEEP_RECENT_CHAT_TURNS, KEEP_RECENT_INTERNAL_MESSAGES


def _prune_old_chat_messages(conn, session_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(end_msg_id) AS max_end FROM summaries WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    summarized_end = int(row["max_end"]) if row and row["max_end"] else 0
    if summarized_end <= 0:
        return 0

    assistant_rows = conn.execute(
        """SELECT id
           FROM messages
           WHERE session_id = ?
             AND source = 'chat'
             AND role = 'assistant'
           ORDER BY id DESC
           LIMIT ?""",
        (session_id, KEEP_RECENT_CHAT_TURNS),
    ).fetchall()
    if len(assistant_rows) < KEEP_RECENT_CHAT_TURNS:
        return 0

    oldest_kept_assistant_id = int(assistant_rows[-1]["id"])
    previous_assistant = conn.execute(
        """SELECT MAX(id) AS prev_id
           FROM messages
           WHERE session_id = ?
             AND source = 'chat'
             AND role = 'assistant'
             AND id < ?""",
        (session_id, oldest_kept_assistant_id),
    ).fetchone()
    previous_assistant_id = (
        int(previous_assistant["prev_id"])
        if previous_assistant and previous_assistant["prev_id"]
        else 0
    )
    cutoff = min(summarized_end, previous_assistant_id)
    if cutoff <= 0:
        return 0

    cur = conn.execute(
        """DELETE FROM messages
           WHERE session_id = ?
             AND source = 'chat'
             AND id <= ?""",
        (session_id, cutoff),
    )
    return cur.rowcount


def _prune_old_internal_messages(conn, session_id: str, source: str) -> int:
    rows = conn.execute(
        """SELECT id
           FROM messages
           WHERE session_id = ?
             AND source = ?
           ORDER BY id DESC
           LIMIT ?""",
        (session_id, source, KEEP_RECENT_INTERNAL_MESSAGES),
    ).fetchall()
    if len(rows) < KEEP_RECENT_INTERNAL_MESSAGES:
        return 0

    oldest_keep_id = int(rows[-1]["id"])
    cur = conn.execute(
        """DELETE FROM messages
           WHERE session_id = ?
             AND source = ?
             AND id < ?""",
        (session_id, source, oldest_keep_id),
    )
    return cur.rowcount


def _prune_old_disabled_scheduled_tasks(
    conn,
    now: datetime | None = None,
    days: int = 30,
) -> int:
    cutoff = (now or datetime.now()) - timedelta(days=max(1, int(days or 1)))
    cur = conn.execute(
        """DELETE FROM scheduled_tasks
           WHERE enabled = 0
             AND created_at < ?""",
        (cutoff.isoformat(timespec="seconds"),),
    )
    return cur.rowcount
