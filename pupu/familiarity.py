"""Single source of truth for familiarity levels, thresholds, and behavior config."""

# ── Level definitions ──

LEVELS = [
    {"max_score": 15, "name": "认识"},
    {"max_score": 35, "name": "熟悉"},
    {"max_score": 60, "name": "朋友"},
    {"max_score": 85, "name": "恋人未满"},
    {"max_score": 100, "name": "恋人"},
]

PROACTIVE_THRESHOLD = 36
OWNER_SESSION_ID = "owner"
OWNER_MAX_FAMILIARITY_SCORE = 100
NON_OWNER_MAX_FAMILIARITY_SCORE = 60


def score_to_level(score: int) -> str:
    for lv in LEVELS:
        if score <= lv["max_score"]:
            return lv["name"]
    return LEVELS[-1]["name"]


def score_to_level_index(score: int) -> int:
    for i, lv in enumerate(LEVELS):
        if score <= lv["max_score"]:
            return i
    return len(LEVELS) - 1


DEFAULT_FAMILIARITY_SCORE = 100
DEFAULT_FAMILIARITY_LEVEL = score_to_level(DEFAULT_FAMILIARITY_SCORE)


def max_familiarity_score(session_id: str = "default") -> int:
    sid = str(session_id or "").strip()
    if sid == OWNER_SESSION_ID:
        return OWNER_MAX_FAMILIARITY_SCORE
    return NON_OWNER_MAX_FAMILIARITY_SCORE


def default_familiarity_score(session_id: str = "default") -> int:
    return max_familiarity_score(session_id)


def clamp_familiarity_score(score: int, session_id: str = "default") -> int:
    try:
        value = int(score)
    except Exception:
        value = default_familiarity_score(session_id)
    return max(0, min(max_familiarity_score(session_id), value))

# ── Proactive messaging frequency (indexed by level index 0-4) ──

PROACTIVE_FREQ_CONFIG = [
    None,  # 0: 认识
    None,  # 1: 熟悉
    {"min_interval": 80, "max_interval": 120, "chance": 0.50},  # 2: 朋友
    {"min_interval": 50, "max_interval": 80, "chance": 0.60},  # 3: 恋人未满
    {"min_interval": 30, "max_interval": 50, "chance": 0.70},  # 4: 恋人
]


def get_proactive_freq(score: int) -> dict | None:
    """Return proactive frequency config for the given score, or None if disabled."""
    if score < PROACTIVE_THRESHOLD:
        return None
    idx = score_to_level_index(score)
    return PROACTIVE_FREQ_CONFIG[idx]
