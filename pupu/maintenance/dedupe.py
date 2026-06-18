"""Deterministic dedupe helpers for maintenance."""


def _dedupe_summaries(conn) -> int:
    rows = conn.execute(
        """SELECT session_id, start_msg_id, end_msg_id, MAX(id) AS keep_id
           FROM summaries
           GROUP BY session_id, start_msg_id, end_msg_id
           HAVING COUNT(*) > 1"""
    ).fetchall()
    deleted = 0
    for row in rows:
        cur = conn.execute(
            """DELETE FROM summaries
               WHERE session_id = ?
                 AND start_msg_id = ?
                 AND end_msg_id = ?
                 AND id <> ?""",
            (row["session_id"], row["start_msg_id"], row["end_msg_id"], row["keep_id"]),
        )
        deleted += cur.rowcount
    return deleted


def _dedupe_events(conn) -> int:
    rows = conn.execute(
        """SELECT session_id, date, delta, description, MIN(id) AS keep_id
           FROM events
           GROUP BY session_id, date, delta, description
           HAVING COUNT(*) > 1"""
    ).fetchall()
    deleted = 0
    for row in rows:
        cur = conn.execute(
            """DELETE FROM events
               WHERE session_id = ?
                 AND date = ?
                 AND delta = ?
                 AND description = ?
                 AND id <> ?""",
            (
                row["session_id"],
                row["date"],
                row["delta"],
                row["description"],
                row["keep_id"],
            ),
        )
        deleted += cur.rowcount
    return deleted


def _dedupe_scheduled_tasks(conn) -> int:
    rows = conn.execute(
        """SELECT MIN(id) AS keep_id, GROUP_CONCAT(id) AS all_ids
           FROM scheduled_tasks
           WHERE enabled = 1
           GROUP BY
             session_id,
             title,
             instruction,
             run_at,
             repeat_kind,
             COALESCE(interval_seconds, -1)
           HAVING COUNT(*) > 1"""
    ).fetchall()
    disabled = 0
    for row in rows:
        keep_id = int(row["keep_id"])
        all_ids = [
            int(part)
            for part in str(row["all_ids"]).split(",")
            if part and int(part) != keep_id
        ]
        if not all_ids:
            continue
        placeholders = ",".join("?" for _ in all_ids)
        print(
            "[pupu][scheduled-debug] maintenance_dedupe_candidates "
            f"keep_id={keep_id} disable_ids={all_ids}"
        )
        cur = conn.execute(
            f"UPDATE scheduled_tasks SET enabled = 0 WHERE id IN ({placeholders})",
            all_ids,
        )
        print(
            "[pupu][scheduled-debug] maintenance_dedupe_applied "
            f"keep_id={keep_id} disabled_rowcount={cur.rowcount}"
        )
        disabled += cur.rowcount
    return disabled
