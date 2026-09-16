"""Metrics for measuring memory density without inspecting model output."""

from __future__ import annotations


def measure_memory_density(conn) -> dict[str, float | int]:
    fact_row = conn.execute(
        """SELECT COUNT(*) AS facts,
                  COALESCE(SUM(LENGTH(fact_key) + LENGTH(fact_value)), 0) AS chars,
                  COALESCE(SUM(
                      CASE WHEN EXISTS (
                          SELECT 1 FROM fact_evidence fe WHERE fe.fact_id = pf.id
                      ) THEN 1 ELSE 0 END
                  ), 0) AS with_evidence
           FROM person_facts pf
           WHERE status = 'active'"""
    ).fetchone()
    event_row = conn.execute(
        """SELECT COUNT(*) AS threads,
                  COALESCE(SUM(
                      LENGTH(title) + LENGTH(followup_hint) +
                      LENGTH(rollup_summary) +
                      LENGTH(COALESCE((
                          SELECT summary FROM event_steps es
                          WHERE es.id = event_threads.current_step_id
                      ), ''))
                  ), 0) AS chars
           FROM event_threads
           WHERE merged_into_thread_id IS NULL AND status != 'merged'"""
    ).fetchone()
    step_row = conn.execute(
        """SELECT COUNT(*) AS steps,
                  COALESCE(SUM(LENGTH(summary) + LENGTH(cause) + LENGTH(reflection)), 0)
                     AS chars
           FROM event_steps"""
    ).fetchone()
    rollup_count = int(
        conn.execute("SELECT COUNT(*) AS c FROM event_thread_rollups").fetchone()["c"]
    )
    archived_messages = int(
        conn.execute("SELECT COUNT(*) AS c FROM archived_messages").fetchone()["c"]
    )
    active_facts = int(fact_row["facts"] or 0)
    fact_evidence_coverage = (
        float(fact_row["with_evidence"] or 0) / active_facts if active_facts else 1.0
    )
    projection_chars = int(fact_row["chars"] or 0) + int(event_row["chars"] or 0)
    raw_evidence_chars = int(step_row["chars"] or 0)
    return {
        "active_facts": active_facts,
        "active_event_threads": int(event_row["threads"] or 0),
        "event_steps": int(step_row["steps"] or 0),
        "event_rollups": rollup_count,
        "archived_messages": archived_messages,
        "fact_evidence_coverage": round(fact_evidence_coverage, 4),
        "projection_chars": projection_chars,
        "raw_evidence_chars": raw_evidence_chars,
        "compression_ratio": round(
            raw_evidence_chars / max(1, projection_chars),
            4,
        ),
    }
