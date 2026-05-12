"""User-facing formatting helpers for long-term facts."""

from __future__ import annotations

from .memory_index import format_memu_facts_report
from .memory import get_self_facts, get_user_facts


def _format_fact_section(title: str, facts: dict[str, str]) -> list[str]:
    lines = [f"{title} {len(facts)} 条"]
    if not facts:
        lines.append("（空）")
        return lines

    for index, (key, value) in enumerate(facts.items(), start=1):
        lines.append(f"{index}. {key}: {value}")
    return lines


def format_facts_report(session_id: str) -> str:
    memu_report = format_memu_facts_report(session_id)
    if memu_report is not None:
        return memu_report

    user_facts = get_user_facts(session_id)
    self_facts = get_self_facts(session_id)

    if not user_facts and not self_facts:
        return "当前没有长期 facts 记忆。"

    lines = []
    lines.extend(_format_fact_section("用户 facts", user_facts))
    lines.append("")
    lines.extend(_format_fact_section("仆仆 self_facts", self_facts))
    return "\n".join(lines)
