"""Stable `message_source` values for persistence and scheduler dispatch."""

# Normal user-driven turn (batch review runs on this source only in agent.chat).
CHAT = "chat"

# Due DB scheduled_tasks tick → synthetic user message to chat().
SCHEDULED = "scheduled"

# Idle proactive assistant line (persisted on assistant message).
PROACTIVE = "proactive"

# In-memory timer nudge turn; legacy scheduled_tasks titles use this prefix.
WAIT_FOLLOWUP = "wait_followup"
