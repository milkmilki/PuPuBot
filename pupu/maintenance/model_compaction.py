"""Model-assisted memory compaction helpers."""

import json
from datetime import datetime

from ..llm import MODEL, get_client
from .parsing import _parse_json_object
from .prompt import MAINTENANCE_PROMPT
from .snapshot import _normalize_int_list, _should_run_model_compaction


def _run_model_compaction(conn, snapshot: dict) -> dict:
    if not _should_run_model_compaction(snapshot):
        return {
            "dropped_summaries": 0,
            "dropped_events": 0,
            "merged_summaries": 0,
            "note": "",
        }

    client = get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=MAINTENANCE_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(snapshot, ensure_ascii=False, indent=2),
            }
        ],
    )
    raw_text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    result = _parse_json_object(raw_text)

    summary_rows = {row["id"]: row for row in snapshot["summaries"]}
    event_rows = {row["id"]: row for row in snapshot["events"]}
    drop_summary_ids = _normalize_int_list(
        result.get("drop_summary_ids", []),
        set(summary_rows.keys()),
    )
    drop_event_ids = _normalize_int_list(
        result.get("drop_event_ids", []),
        set(event_rows.keys()),
    )
    merged_summary = str(result.get("merged_summary", "")).strip()
    note = str(result.get("notes", "")).strip()

    merged_saved = 0
    if merged_summary and len(drop_summary_ids) >= 2:
        source_rows = [summary_rows[summary_id] for summary_id in drop_summary_ids]
        start_msg_id = min(row["start_msg_id"] for row in source_rows)
        end_msg_id = max(row["end_msg_id"] for row in source_rows)
        conn.execute(
            """INSERT INTO summaries
               (session_id, summary, start_msg_id, end_msg_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot["session_id"],
                merged_summary,
                start_msg_id,
                end_msg_id,
                datetime.now().isoformat(),
            ),
        )
        merged_saved = 1

    dropped_summaries = 0
    if drop_summary_ids:
        placeholders = ",".join("?" for _ in drop_summary_ids)
        cur = conn.execute(
            f"DELETE FROM summaries WHERE id IN ({placeholders})",
            drop_summary_ids,
        )
        dropped_summaries = cur.rowcount

    dropped_events = 0
    if drop_event_ids:
        placeholders = ",".join("?" for _ in drop_event_ids)
        cur = conn.execute(
            f"DELETE FROM events WHERE id IN ({placeholders})",
            drop_event_ids,
        )
        dropped_events = cur.rowcount

    return {
        "dropped_summaries": dropped_summaries,
        "dropped_events": dropped_events,
        "merged_summaries": merged_saved,
        "note": note,
    }
