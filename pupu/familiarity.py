"""Single source of truth for familiarity levels, thresholds, and behavior config."""

import random

# ── Level definitions ──

LEVELS = [
    {"max_score": 15, "name": "认识"},
    {"max_score": 35, "name": "熟悉"},
    {"max_score": 60, "name": "朋友"},
    {"max_score": 85, "name": "恋人未满"},
    {"max_score": 100, "name": "恋人"},
]

PROACTIVE_THRESHOLD = 36


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


# ── Reply delay config (indexed by level index 0-4) ──

REPLY_DELAY_CONFIG = [
    # 0: 认识
    {"delay_chance": 0.30, "delay_range": (30, 120),
     "terse_chance": 0.15, "terse_replies": ["...", "嗯", "哦"]},
    # 1: 熟悉
    {"delay_chance": 0.20, "delay_range": (15, 60),
     "terse_chance": 0.08, "terse_replies": ["...", "嗯", "嗯嗯"]},
    # 2: 朋友
    {"delay_chance": 0.10, "delay_range": (5, 30),
     "terse_chance": 0, "terse_replies": []},
    # 3: 恋人未满
    {"delay_chance": 0.05, "delay_range": (2, 10),
     "terse_chance": 0, "terse_replies": []},
    # 4: 恋人
    {"delay_chance": 0.05, "delay_range": (2, 10),
     "terse_chance": 0, "terse_replies": []},
]

# ── Proactive messaging frequency (indexed by level index 0-4) ──

PROACTIVE_FREQ_CONFIG = [
    None,  # 0: 认识
    None,  # 1: 熟悉
    {"min_interval": 80, "max_interval": 120, "chance": 0.50},  # 2: 朋友
    {"min_interval": 50, "max_interval": 80, "chance": 0.60},  # 3: 恋人未满
    {"min_interval": 30, "max_interval": 50, "chance": 0.70},  # 4: 恋人
]


# ── Convenience functions ──

def compute_reply_delay(score: int) -> tuple[float, str | None]:
    """Return (delay_seconds, replacement_text_or_None) based on familiarity."""
    idx = score_to_level_index(score)
    cfg = REPLY_DELAY_CONFIG[idx]
    r = random.random()
    if cfg["terse_chance"] and r < cfg["terse_chance"]:
        return 0, random.choice(cfg["terse_replies"])
    r2 = random.random()
    if cfg["delay_chance"] and r2 < cfg["delay_chance"]:
        lo, hi = cfg["delay_range"]
        return random.uniform(lo, hi), None
    return 0, None


def get_proactive_freq(score: int) -> dict | None:
    """Return proactive frequency config for the given score, or None if disabled."""
    if score < PROACTIVE_THRESHOLD:
        return None
    idx = score_to_level_index(score)
    return PROACTIVE_FREQ_CONFIG[idx]
