"""Stable `message_source` values for persistence and scheduler dispatch."""

# Normal user-driven turn (batch review runs on this source only in agent.chat).
CHAT = "chat"

# Due DB scheduled_tasks tick -> synthetic user message to chat().
SCHEDULED = "scheduled"

# Idle proactive assistant line (persisted on assistant message).
PROACTIVE = "proactive"

# In-memory timer nudge turn; legacy scheduled_tasks titles use this prefix.
WAIT_FOLLOWUP = "wait_followup"


def normalize_message_source(source: object) -> str:
    return str(source or CHAT).strip().lower() or CHAT


def message_source_label(
    role: object,
    source: object,
    character_name: str,
    *,
    user_label: str = "用户",
    assistant_label: str = "",
) -> str:
    src = normalize_message_source(source)
    msg_role = str(role or "").strip().lower()
    assistant = str(assistant_label or character_name or "助手").strip() or "助手"
    character = str(character_name or assistant).strip() or assistant
    if src == SCHEDULED:
        return "系统触发的定时任务" if msg_role == "user" else f"{assistant}响应定时任务"
    if src == WAIT_FOLLOWUP:
        return f"系统触发的追问（{character}）" if msg_role == "user" else f"{assistant}响应追问"
    if src == PROACTIVE:
        return f"{assistant}主动发出"
    if msg_role == "user":
        return user_label
    return assistant


def is_internal_message_source(source: object) -> bool:
    return normalize_message_source(source) in {SCHEDULED, PROACTIVE, WAIT_FOLLOWUP}
