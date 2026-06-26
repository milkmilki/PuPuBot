"""Built-in semantic index facade for PuPu memory recall."""

from .core import (
    SemanticWriteResult,
    clear_semantic_index,
    clear_semantic_session,
    format_semantic_event_threads_report,
    format_semantic_facts_report,
    format_semantic_recall_report,
    is_semantic_index_enabled,
    rebuild_source_cache,
    recall_memories,
    reconcile_source_cache,
    sync_missing_event_threads,
    sync_review_memory,
)
from .tidy import (
    analyze_semantic_tidy,
    format_semantic_tidy_report,
    run_semantic_maintenance,
    run_semantic_tidy,
)

__all__ = [
    "SemanticWriteResult",
    "analyze_semantic_tidy",
    "clear_semantic_index",
    "clear_semantic_session",
    "format_semantic_event_threads_report",
    "format_semantic_facts_report",
    "format_semantic_recall_report",
    "format_semantic_tidy_report",
    "is_semantic_index_enabled",
    "rebuild_source_cache",
    "recall_memories",
    "reconcile_source_cache",
    "run_semantic_maintenance",
    "run_semantic_tidy",
    "sync_missing_event_threads",
    "sync_review_memory",
]
