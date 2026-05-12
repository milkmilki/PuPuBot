"""Long-term memory index facade.

memU is optional at runtime. Callers should use these helpers instead of
importing memU directly so PuPu can keep chatting when the index is unavailable.
"""

from .memu_adapter import (
    clear_memu_session,
    format_memu_facts_report,
    format_memu_important_events_report,
    format_memu_recall_report,
    is_memu_long_term_enabled,
    recall_memories,
    rebuild_memu_session,
    run_memu_maintenance,
    sync_review_memory,
)

__all__ = [
    "clear_memu_session",
    "format_memu_facts_report",
    "format_memu_important_events_report",
    "format_memu_recall_report",
    "is_memu_long_term_enabled",
    "recall_memories",
    "rebuild_memu_session",
    "run_memu_maintenance",
    "sync_review_memory",
]
