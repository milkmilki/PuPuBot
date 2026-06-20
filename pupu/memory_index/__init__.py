"""Long-term memory index facade.

memU is optional at runtime. Callers should use these helpers instead of
importing memU directly so PuPu can keep chatting when the index is unavailable.
"""

from .memu_adapter import (
    clear_memu_session,
    format_memu_facts_report,
    format_memu_event_threads_report,
    format_memu_recall_report,
    is_memu_long_term_enabled,
    recall_memories,
    rebuild_memu_source_cache,
    reconcile_memu_source_cache,
    sync_missing_memu_event_threads,
    sync_review_memory,
)
from .memu_tidy import analyze_memu_tidy, format_memu_tidy_report, run_memu_maintenance, run_memu_tidy

__all__ = [
    "clear_memu_session",
    "format_memu_facts_report",
    "format_memu_event_threads_report",
    "format_memu_recall_report",
    "format_memu_tidy_report",
    "analyze_memu_tidy",
    "is_memu_long_term_enabled",
    "recall_memories",
    "rebuild_memu_source_cache",
    "reconcile_memu_source_cache",
    "run_memu_maintenance",
    "run_memu_tidy",
    "sync_missing_memu_event_threads",
    "sync_review_memory",
]
