"""Proactive messaging: pupu initiates conversations based on time of day."""

import asyncio
import random
from datetime import datetime, timedelta

from .followup import DIALOGUE_OUTPUT_PROTOCOL, _parse_dialogue_output
from .llm import JUDGE_MODEL, MODEL, chat_complete
from .tools import execute_tool, get_proactive_tool_definitions
from .familiarity import get_proactive_freq, score_to_level, PROACTIVE_THRESHOLD
from .event_thread_context import format_event_threads_section
from .memory import (
    get_familiarity,
    get_event_threads,
    get_last_message_time,
    get_person_facts,
    get_recent_messages,
    get_summaries,
    person_from_session,
    save_message,
)
from .memory_index import is_memu_long_term_enabled, recall_memories
from .message_sources import CHAT, PROACTIVE, SCHEDULED, WAIT_FOLLOWUP
from .persona import FAMILIARITY_PROMPTS, PROACTIVE_PROMPT, get_pupu_name
from .proactive_control import is_proactive_enabled
from .sessions import OWNER_SESSION
from .storage.facts import group_person_facts_for_display

PROACTIVE_HISTORY_LIMIT = 30
PROACTIVE_SUMMARY_LIMIT = 2

TIME_PERIODS = [
    {"name": "凌晨到早上", "start": 0, "end": 9, "topics": [
        "说一个你刚刚做的梦的具体内容，可以是荒诞的",
        "抱怨一个具体的起床困难原因，比如昨晚熬夜看了什么",
        "分享今天天气怎样，吐槽或者期待",
        "提一个今天想做的具体事情",
        "说一个你深夜突然想到的具体问题或疑惑",
        "聊一个你对今天的具体想法或期待",
    ]},
    {"name": "白天", "start": 9, "end": 19, "topics": [
        "推荐一道你最近吃到的具体好吃的菜，说为什么好吃",
        "吐槽一个具体的外卖/食堂踩雷经历",
        "说你想吃某个具体的东西但吃不到",
        "分享一个美食冷知识或者做菜小技巧",
        "分享一个你刚发现的具体有趣网站/工具/App，说它能干什么",
        "说一个你刚看到的具体新闻或热搜，发表你的看法",
        "分享一个具体的技术知识点或编程冷知识",
        "说一个你最近在研究的具体东西，比如某个算法、某个框架",
        "吐槽一个你遇到的具体bug或者坑",
        "推荐一首歌或一个视频，说为什么觉得好",
        "说你今天具体做了什么，发生了什么事",
        "抱怨一个具体让你累的事情",
        "说一个今晚打算做的具体事情，比如看某部剧、打某个游戏",
        "问用户今天过得怎么样，顺便说说自己的",
    ]},
    {"name": "晚上闲聊", "start": 19, "end": 24, "topics": [
        "推荐一本你在看的具体的书，说书里某个让你印象深刻的观点或情节",
        "推荐一部具体的动漫/电影/剧，说为什么好看",
        "分享一个让你有感触的具体想法或感悟",
        "说一个你最近学到的有意思的事实或知识",
        "聊一个你感兴趣的具体话题，比如某个游戏、某个圈子的事",
        "分享一段你觉得很有意思的对话或段子",
        "说一个你深夜突然想到的具体问题或疑惑",
        "分享一句让你有感触的歌词或台词，说为什么触动你",
        "聊一个你对未来的具体想法或期待",
        "说一件你最近在纠结的具体事情",
        "感慨一件具体的事情或回忆",
    ]},
]


def _get_current_period() -> dict | None:
    now = datetime.now()
    hour = now.hour + now.minute / 60.0
    for p in TIME_PERIODS:
        if p["start"] <= hour < p["end"]:
            return p
    return None


def _is_quiet_hours() -> bool:
    """Do not proactively message at night.

    Night is defined as 00:00-06:00 local time.
    """
    hour = datetime.now().hour
    return hour < 6


def _had_recent_chat_within(minutes: int) -> bool:
    """Return True if this session had any message within the given minutes."""
    ts = get_last_message_time(OWNER_SESSION)
    if not ts:
        return False
    try:
        last_dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return datetime.now() - last_dt < timedelta(minutes=minutes)


def _minutes_since_last_chat() -> int | None:
    """Return minutes since last message in owner session, or None if no history."""
    ts = get_last_message_time(OWNER_SESSION)
    if not ts:
        return None
    try:
        last_dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    delta = datetime.now() - last_dt
    if delta.total_seconds() < 0:
        return 0
    return int(delta.total_seconds() // 60)


def _truncate_debug(text: str, limit: int = 240) -> str:
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _replace_default_character_name(text: object, character_name: str) -> str:
    value = str(text or "")
    if character_name and character_name != "仆仆":
        value = value.replace("仆仆", character_name)
    return value


def _load_proactive_context() -> tuple[list[dict], list[dict]]:
    return (
        get_recent_messages(PROACTIVE_HISTORY_LIMIT, OWNER_SESSION),
        get_summaries(OWNER_SESSION, limit=PROACTIVE_SUMMARY_LIMIT),
    )


def _format_recent_context(recent: list[dict]) -> str:
    if not recent:
        return "（暂无历史对话）"

    character_name = get_pupu_name()
    lines = []
    for message in recent:
        who = _recent_message_label(message, character_name)
        content = str(message.get("content") or "").strip()
        if content:
            lines.append(f"{who}: {content}")
    return "\n".join(lines) if lines else "（暂无历史对话）"


def _recent_message_label(message: dict, character_name: str) -> str:
    source = str(message.get("source") or CHAT).strip().lower()
    role = str(message.get("role") or "").strip().lower()
    if source == SCHEDULED:
        return "系统触发的定时任务"
    if source == WAIT_FOLLOWUP:
        return f"系统触发的追问（{character_name}）"
    if source == PROACTIVE:
        return f"{character_name}主动发出"
    if role == "user":
        return "用户"
    return character_name


def _format_summary_context(summaries: list[dict]) -> str:
    lines = []
    for item in summaries:
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines) if lines else "（暂无摘要）"


def _format_person_facts_section(facts: list[dict], character_name: str) -> str:
    groups = group_person_facts_for_display(facts or [])
    if not groups:
        return ""
    lines = ["## 长期 facts（按人物）"]
    for label, rows in groups:
        label = _replace_default_character_name(label, character_name)
        lines.append(f"{label}:")
        for row in rows:
            key = _replace_default_character_name(row.get("fact_key"), character_name)
            value = _replace_default_character_name(row.get("fact_value"), character_name)
            if key and value:
                lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _build_proactive_memu_query(score: int, period: dict, recent: list[dict]) -> str:
    character_name = get_pupu_name()
    recent_lines = []
    for item in recent[-4:]:
        role = _recent_message_label(item, character_name)
        content = str(item.get("content") or "").replace("\n", " ").strip()
        if content:
            recent_lines.append(f"{role}: {content[:120]}")
    recent_text = " | ".join(recent_lines) or "无"
    return (
        f"主动消息记忆召回。当前实例名是{character_name}，对话对象统一称为用户。"
        f" 当前时段：{period['name']}。好感度：{score}。"
        f" 最近聊天：{recent_text}。"
        " 请召回和当前情境、用户近况、未解决话题、值得自然跟进的事相关的长期记忆。"
    )


def _format_recalled_memories_section(memories: list[dict]) -> str:
    character_name = get_pupu_name()
    lines = []
    for item in memories:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        kind = str(item.get("kind") or "memory").strip()
        if kind in {"summary", "event_thread"}:
            subject = f"用户 / {character_name}"
        elif kind == "person_fact":
            subject = "相关人物"
        else:
            subject = "相关记忆"
        score = item.get("score")
        score_text = f" score={float(score):.3f}" if isinstance(score, (int, float)) else ""
        created_at = item.get("created_at")
        created_text = f" created_at={created_at}" if created_at else ""
        lines.append(f"- [{kind} | {subject}] {text}{score_text}{created_text}")
    if not lines:
        return ""
    return f"## 自然想起的长期记忆（用户 / {character_name}）\n" + "\n".join(lines)


def _recall_proactive_memories(score: int, period: dict, recent: list[dict]) -> list[dict]:
    if not is_memu_long_term_enabled():
        return []
    query = _build_proactive_memu_query(score, period, recent)
    print(
        f"[pupu][proactive] phase=memu_recall start score={score} "
        f"period={period['name']} recent_messages={len(recent)} query_chars={len(query)}"
    )
    try:
        memories = recall_memories(
            query=query,
            context_session=OWNER_SESSION,
            identity_session=OWNER_SESSION,
            history=recent,
            limit=4,
        )
    except Exception as exc:
        print(f"[pupu][proactive] phase=memu_recall failed error={type(exc).__name__}: {exc}")
        return []
    print(f"[pupu][proactive] phase=memu_recall done count={len(memories)}")
    return memories


def _model_should_proactively_reach_out(score: int, period: dict, idle_minutes: int | None) -> bool:
    """Ask model whether to send a proactive message now.

    The model must answer with one word: SEND or WAIT.
    """
    try:
        print(
            f"[pupu][proactive] phase=judge start score={score} "
            f"period={period['name']} idle_minutes={idle_minutes}"
        )
        recent, summaries = _load_proactive_context()
        recent_text = _format_recent_context(recent)
        summary_text = _format_summary_context(summaries)

        idle_desc = "无历史" if idle_minutes is None else f"{idle_minutes}分钟"
        now_str = datetime.now().strftime("%H:%M")

        character_name = get_pupu_name()
        system_prompt = (
            f"你是{character_name}的主动消息调度决策器。"
            "你只负责判断当前是否应该主动找用户聊天。"
            "规则：若当前适合主动找用户，输出 SEND；否则输出 WAIT。"
            "禁止输出解释、标点或其他文字。"
        )

        user_prompt = (
            f"当前时间: {now_str}\n"
            f"当前时段: {period['name']}\n"
            f"当前好感度: {score}\n"
            f"距离上次聊天: {idle_desc}\n"
            f"最近摘要:\n{summary_text}\n"
            f"最近聊天:\n{recent_text}\n"
            "现在是否要主动找用户？只输出 SEND 或 WAIT。"
        )

        text = chat_complete(
            role="judge",
            model=JUDGE_MODEL,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1024,
        )
        decision = text.strip().upper()
        print(
            f"[pupu][proactive] phase=judge done decision={decision[:32] or 'EMPTY'} "
            f"raw={_truncate_debug(text, 180)}"
        )
        return decision.startswith("SEND")
    except Exception as e:
        print(f"[pupu][proactive] phase=judge failed error={e}")
        return False


def _build_proactive_prompt(score: int, period: dict) -> str:
    level = score_to_level(score)
    level_desc = FAMILIARITY_PROMPTS[level]
    character_name = get_pupu_name()

    recent, summaries = _load_proactive_context()
    recent_ctx = _format_recent_context(recent)
    summary_ctx = _format_summary_context(summaries)

    topic = random.choice(period["topics"])

    if is_memu_long_term_enabled():
        recalled_memories = _recall_proactive_memories(score, period, recent)
        prompt = PROACTIVE_PROMPT.format(
            character_name=character_name,
            persona_level=level_desc,
            facts_section="",
            summary_context=summary_ctx,
            time_period=period["name"],
            time_desc=f"{datetime.now().strftime('%H:%M')}",
            topic_hint=topic,
            recent_context=recent_ctx,
        )
        recalled_section = _format_recalled_memories_section(recalled_memories)
        if recalled_section:
            prompt += (
                "\n\n"
                + recalled_section
                + "\n这些是当前场景里自然想起的长期记忆，可以顺着当前情境轻轻提一句，但不要机械复述。"
            )
        return prompt

    event_threads = get_event_threads(OWNER_SESSION, limit=4)
    person_facts = get_person_facts(
        subject_person_keys=["instance", person_from_session(OWNER_SESSION)],
        include_relationships=True,
    )
    facts_section = _format_person_facts_section(person_facts, character_name)

    prompt = PROACTIVE_PROMPT.format(
        character_name=character_name,
        persona_level=level_desc,
        facts_section=facts_section,
        summary_context=summary_ctx,
        time_period=period["name"],
        time_desc=f"{datetime.now().strftime('%H:%M')}",
        topic_hint=topic,
        recent_context=recent_ctx,
    )
    event_threads_section = format_event_threads_section(
        event_threads,
        heading=f"## {character_name}最近会自然记着的事",
        subject_label=f"用户 / {character_name}",
        character_name=character_name,
    )
    if event_threads_section:
        prompt += (
            "\n\n"
            + event_threads_section
            + "\n如果其中有临近的事、刚说好的事、或你已经设过提醒但还想自然关心一下，可以顺着当前情境轻轻提一句。"
        )
    return prompt

def generate_proactive_message(score: int, period: dict) -> str | None:
    """Generate a proactive message using Claude API with web search capability."""
    from .dialogue_loop import cancel_wait_timer, schedule_wait_timer

    cancel_wait_timer(OWNER_SESSION)
    try:
        prompt = _build_proactive_prompt(score, period)
        print(
            f"[pupu][proactive] phase=generate start score={score} "
            f"period={period['name']} prompt={_truncate_debug(prompt, 180)}"
        )
        messages = [{"role": "user", "content": "（主动给用户发一条消息。如果话题需要具体内容，可以先搜索一下再聊。）"}]

        def _tool_handler(tool_name: str, tool_input: dict, reason_hint: str | None = None):
            result = execute_tool(
                tool_name,
                tool_input,
                session_id=OWNER_SESSION,
                reason_hint=reason_hint or None,
            )
            if isinstance(result, str) and len(result) > 2000:
                return result[:2000] + "...(截断)"
            return result

        text = chat_complete(
            role="proactive",
            model=MODEL,
            system=prompt + DIALOGUE_OUTPUT_PROTOCOL,
            messages=messages,
            max_tokens=5000,
            tools=get_proactive_tool_definitions(),
            tool_handler=_tool_handler,
            session_id=OWNER_SESSION,
            is_admin=False,
            tool_exposure="proactive",
        )

        print(f"[pupu][proactive] phase=generate done text={_truncate_debug(text, 220)}")
        content, should_wait = _parse_dialogue_output(text or "")
        if content:
            save_message("assistant", content, OWNER_SESSION, source=PROACTIVE)
        if should_wait:
            schedule_wait_timer(OWNER_SESSION)
        return content or None
    except Exception as e:
        print(f"[pupu][proactive] phase=generate failed error={e}")
        return None


async def proactive_loop(send_func):
    """Main proactive messaging loop. send_func(text) should send a private message to owner."""
    if not is_proactive_enabled():
        print("[pupu] proactive messaging stopped (disabled by switch)")
        return
    print("[pupu] proactive messaging started")
    try:
        while True:
            if not is_proactive_enabled():
                print("[pupu][proactive] loop stop=disabled_by_switch")
                break
            score = get_familiarity(OWNER_SESSION)
            freq = get_proactive_freq(score)

            print(f"[pupu][proactive] loop score={score} freq={freq}")

            if freq is None:
                print("[pupu][proactive] skip=no_proactive_frequency")
                await asyncio.sleep(300)
                continue

            interval = random.uniform(freq["min_interval"], freq["max_interval"]) * 60
            print(
                f"[pupu][proactive] sleep seconds={int(interval)} "
                f"window={freq['min_interval']}-{freq['max_interval']}min"
            )
            await asyncio.sleep(interval)

            if _is_quiet_hours():
                print("[pupu][proactive] skip=quiet_hours")
                continue

            if _had_recent_chat_within(60):
                print("[pupu][proactive] skip=recent_chat_within_60min")
                continue

            period = _get_current_period()
            if period is None:
                print("[pupu][proactive] skip=no_current_period")
                continue

            score = get_familiarity(OWNER_SESSION)
            if score < PROACTIVE_THRESHOLD:
                print(f"[pupu][proactive] skip=low_score score={score} threshold={PROACTIVE_THRESHOLD}")
                continue

            idle_minutes = _minutes_since_last_chat()
            print(
                f"[pupu][proactive] phase=decision_context score={score} "
                f"period={period['name']} idle_minutes={idle_minutes}"
            )
            should_send = await asyncio.to_thread(
                _model_should_proactively_reach_out,
                score,
                period,
                idle_minutes,
            )
            print(f"[pupu][proactive] phase=judge_result should_send={should_send}")
            if not should_send:
                print("[pupu][proactive] skip=model_decided_wait")
                continue

            text = await asyncio.to_thread(generate_proactive_message, score, period)
            if text:
                try:
                    await send_func(text)
                    print(f"[pupu] proactive >>> {text[:80]}")
                except Exception as e:
                    print(f"[pupu][proactive] phase=send failed error={e}")
            else:
                print("[pupu][proactive] skip=no_generated_text")
    except asyncio.CancelledError:
        print("[pupu] proactive messaging stopped")
