"""User-facing formatting helpers for long-term facts."""

from __future__ import annotations

from .memory import (
    get_person_facts,
    group_person_facts_for_display,
    person_from_session,
)


def _format_fact_section(title: str, rows: list[dict]) -> list[str]:
    lines = [f"{title} facts {len(rows)} 条"]
    if not rows:
        lines.append("（空）")
        return lines
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['fact_key']}: {row['fact_value']}")
    return lines


def format_facts_report(session_id: str) -> str:
    subject_key = person_from_session(session_id)
    facts = get_person_facts(
        subject_person_keys=[subject_key, "instance"],
        include_relationships=True,
    )
    if not facts:
        return "当前没有长期 facts 记忆。"

    lines: list[str] = []
    for title, rows in group_person_facts_for_display(facts):
        if lines:
            lines.append("")
        lines.extend(_format_fact_section(title, rows))
    return "\n".join(lines)
