"""User-facing formatting helpers for long-term facts."""

from __future__ import annotations

from .memory import (
    get_person_facts,
    group_person_facts_for_display,
    person_from_session,
)
from .fact_search import format_related_person_facts


def _format_fact_section(title: str, rows: list[dict]) -> list[str]:
    lines = [f"{title} facts {len(rows)} 条"]
    if not rows:
        lines.append("（空）")
        return lines
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['fact_key']}: {row['fact_value']}")
    return lines


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def format_facts_search_report(session_id: str, query: str, limit: int = 8) -> str:
    query = _compact(query)
    debug = False
    if query.startswith("--debug "):
        debug = True
        query = _compact(query.removeprefix("--debug "))
    elif query == "--debug":
        debug = True
        query = ""
    return format_related_person_facts(
        query,
        identity_session=session_id,
        context_session=session_id,
        limit=limit,
        debug=debug,
    )


def format_facts_report(session_id: str, query: str = "") -> str:
    query = _compact(query)
    if query:
        command, _, rest = query.partition(" ")
        if command.lower() in {"search", "find", "query", "查找", "搜索"}:
            return format_facts_search_report(session_id, rest)
        return format_facts_search_report(session_id, query)

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
