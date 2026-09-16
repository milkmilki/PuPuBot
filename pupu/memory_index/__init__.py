"""Long-term semantic memory index facade.

SQLite remains PuPu's source of truth. This package exposes the rebuildable
semantic index cache used for RAG recall.
"""

from ..semantic_index import (
    SemanticWriteResult,
    analyze_semantic_tidy,
    clear_semantic_index,
    clear_semantic_session,
    format_semantic_event_threads_report,
    format_semantic_facts_report,
    format_semantic_recall_report,
    format_semantic_tidy_report,
    is_semantic_index_enabled,
    recall_memories,
    rebuild_source_cache,
    reconcile_source_cache,
    run_semantic_maintenance,
    run_semantic_tidy,
    sync_missing_event_threads,
    sync_review_memory,
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
    "recall_memories",
    "rebuild_source_cache",
    "reconcile_source_cache",
    "run_semantic_maintenance",
    "run_semantic_tidy",
    "sync_missing_event_threads",
    "sync_review_memory",
]
