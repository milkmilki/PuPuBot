"""Proactive messaging: pupu initiates conversations based on time of day."""

import asyncio
import random
from datetime import datetime, timedelta

from .followup import DIALOGUE_OUTPUT_PROTOCOL, _parse_dialogue_output
from .llm import JUDGE_MODEL, MODEL, chat_complete
from .tools import PROACTIVE_TOOL_DEFINITIONS, execute_tool
from .familiarity import get_proactive_freq, score_to_level, PROACTIVE_THRESHOLD
from .important_event_context import format_important_events_section
from .memory import (
    get_familiarity,
    get_important_events,
    get_last_message_time,
    get_recent_messages,
    get_self_facts,
    get_user_facts,
    save_message,
)
from .persona import FAMILIARITY_PROMPTS, PROACTIVE_PROMPT

OWNER_SESSION = "owner"

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


def _model_should_proactively_reach_out(score: int, period: dict, idle_minutes: int | None) -> bool:
    """Ask model whether to send a proactive message now.

    The model must answer with one word: SEND or WAIT.
    """
    try:
        print(
            f"[pupu][proactive] phase=judge start score={score} "
            f"period={period['name']} idle_minutes={idle_minutes}"
        )
        recent = get_recent_messages(4, OWNER_SESSION)
        if recent:
            recent_text = "\n".join(
                f"{'用户' if m['role'] == 'user' else '你'}: {m['content'][:60]}"
                for m in recent[-6:]
            )
        else:
            recent_text = "（暂无历史对话）"

        idle_desc = "无历史" if idle_minutes is None else f"{idle_minutes}分钟"
        now_str = datetime.now().strftime("%H:%M")

        system_prompt = (
            "你是仆仆的主动消息调度决策器。"
            "你只负责判断当前是否应该主动找用户聊天。"
            "规则：若当前适合主动找用户，输出 SEND；否则输出 WAIT。"
            "禁止输出解释、标点或其他文字。"
        )

        user_prompt = (
            f"当前时间: {now_str}\n"
            f"当前时段: {period['name']}\n"
            f"当前好感度: {score}\n"
            f"距离上次聊天: {idle_desc}\n"
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

    self_facts = get_self_facts(OWNER_SESSION)
    important_events = get_important_events(OWNER_SESSION, limit=4)
    user_facts = get_user_facts(OWNER_SESSION)
    recent = get_recent_messages(4, OWNER_SESSION)

    sf_section = ""
    if self_facts:
        sf_section = "关于你自己（你之前说过的设定）：\n" + "\n".join(
            f"- {k}：{v}" for k, v in self_facts.items()
        )

    uf_section = ""
    if user_facts:
        uf_section = "你对这个人的了解：\n" + "\n".join(
            f"- {k}：{v}" for k, v in user_facts.items()
        )

    recent_ctx = "（还没怎么聊过）"
    if recent:
        lines = []
        for m in recent[-5:]:
            who = "用户" if m["role"] == "user" else "你"
            lines.append(f"{who}: {m['content'][:80]}")
        recent_ctx = "\n".join(lines)

    topic = random.choice(period["topics"])

    prompt = PROACTIVE_PROMPT.format(
        persona_level=level_desc,
        self_facts_section=sf_section,
        user_facts_section=uf_section,
        time_period=period["name"],
        time_desc=f"{datetime.now().strftime('%H:%M')}",
        topic_hint=topic,
        recent_context=recent_ctx,
    )
    important_events_section = format_important_events_section(
        important_events,
        heading="## 你最近会自然记着的事",
    )
    if important_events_section:
        prompt += (
            "\n\n"
            + important_events_section
            + "\n如果其中有临近的事、刚说好的事、或你已经设过提醒但还想自然关心一下，可以顺着当前情境轻轻提一句。"
        )
    return prompt


PROACTIVE_TOOLS = [
    {
        "name": "web_search",
        "description": "搜索网上的内容。想聊具体的书、新闻、技术、热搜、歌曲等时用这个先搜一下，拿到真实内容再聊。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "抓取网页内容。搜到感兴趣的链接后可以用这个看具体内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的URL"},
            },
            "required": ["url"],
        },
    },
]


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
            tools=PROACTIVE_TOOL_DEFINITIONS,
            tool_handler=_tool_handler,
            session_id=OWNER_SESSION,
            is_admin=False,
            tool_exposure="proactive",
        )

        print(f"[pupu][proactive] phase=generate done text={_truncate_debug(text, 220)}")
        content, should_wait = _parse_dialogue_output(text or "")
        if content:
            save_message("assistant", content, OWNER_SESSION, source="proactive")
        if should_wait:
            schedule_wait_timer(OWNER_SESSION)
        return content or None
    except Exception as e:
        print(f"[pupu][proactive] phase=generate failed error={e}")
        return None


async def proactive_loop(send_func):
    """Main proactive messaging loop. send_func(text) should send a private message to owner."""
    print("[pupu] proactive messaging started")
    try:
        while True:
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
