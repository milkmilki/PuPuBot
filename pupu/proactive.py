"""Proactive messaging: pupu initiates conversations based on time of day."""

import asyncio
import random
from datetime import datetime, timedelta

from .llm import JUDGE_MODEL, MODEL, collect_reason_hint, get_client, join_text_blocks
from .tools import PROACTIVE_TOOL_DEFINITIONS, execute_tool
from .familiarity import get_proactive_freq, score_to_level, PROACTIVE_THRESHOLD
from .memory import (
    get_familiarity,
    get_last_message_time,
    get_recent_messages,
    get_self_facts,
    get_user_facts,
    save_message,
)
from .persona import FAMILIARITY_PROMPTS, PROACTIVE_PROMPT

OWNER_SESSION = "owner"

TIME_PERIODS = [
    {"name": "早安", "start": 7, "end": 9, "topics": [
        "说一个你刚刚做的梦的具体内容，可以是荒诞的",
        "抱怨一个具体的起床困难原因，比如昨晚熬夜看了什么",
        "分享今天天气怎样，吐槽或者期待",
        "提一个今天想做的具体事情",
    ]},
    {"name": "午饭", "start": 11.5, "end": 13, "topics": [
        "推荐一道你最近吃到的具体好吃的菜，说为什么好吃",
        "吐槽一个具体的外卖/食堂踩雷经历",
        "说你想吃某个具体的东西但吃不到",
        "分享一个美食冷知识或者做菜小技巧",
    ]},
    {"name": "下午摸鱼", "start": 14, "end": 17, "topics": [
        "分享一个你刚发现的具体有趣网站/工具/App，说它能干什么",
        "说一个你刚看到的具体新闻或热搜，发表你的看法",
        "分享一个具体的技术知识点或编程冷知识",
        "说一个你最近在研究的具体东西，比如某个算法、某个框架",
        "吐槽一个你遇到的具体bug或者坑",
        "推荐一首歌或一个视频，说为什么觉得好",
    ]},
    {"name": "晚饭", "start": 17.5, "end": 19, "topics": [
        "说你今天具体做了什么，发生了什么事",
        "抱怨一个具体让你累的事情",
        "说一个今晚打算做的具体事情，比如看某部剧、打某个游戏",
        "问用户今天过得怎么样，顺便说说自己的",
    ]},
    {"name": "晚上闲聊", "start": 19, "end": 23, "topics": [
        "推荐一本你在看的具体的书，说书里某个让你印象深刻的观点或情节",
        "推荐一部具体的动漫/电影/剧，说为什么好看",
        "分享一个让你有感触的具体想法或感悟",
        "说一个你最近学到的有意思的事实或知识",
        "聊一个你感兴趣的具体话题，比如某个游戏、某个圈子的事",
        "分享一段你觉得很有意思的对话或段子",
    ]},
    {"name": "深夜emo", "start": 23, "end": 25, "topics": [
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
    if hour < 1:
        hour += 24
    for p in TIME_PERIODS:
        if p["start"] <= hour < p["end"]:
            return p
    return None


def _is_quiet_hours() -> bool:
    """Do not proactively message at night.

    Night is defined as 23:00-09:00 local time.
    """
    hour = datetime.now().hour
    return hour >= 23 or hour < 9


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


def _model_should_proactively_reach_out(score: int, period: dict, idle_minutes: int | None) -> bool:
    """Ask model whether to send a proactive message now.

    The model must answer with one word: SEND or WAIT.
    """
    try:
        client = get_client()
        recent = get_recent_messages(6, OWNER_SESSION)
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

        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=16,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text
        decision = text.strip().upper()
        return decision.startswith("SEND")
    except Exception as e:
        print(f"[pupu] proactive decision failed: {e}")
        return False


def _build_proactive_prompt(score: int, period: dict) -> str:
    level = score_to_level(score)
    level_desc = FAMILIARITY_PROMPTS[level]

    self_facts = get_self_facts(OWNER_SESSION)
    user_facts = get_user_facts(OWNER_SESSION)
    recent = get_recent_messages(5, OWNER_SESSION)

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

    return PROACTIVE_PROMPT.format(
        persona_level=level_desc,
        self_facts_section=sf_section,
        user_facts_section=uf_section,
        time_period=period["name"],
        time_desc=f"{datetime.now().strftime('%H:%M')}",
        topic_hint=topic,
        recent_context=recent_ctx,
    )


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
    try:
        client = get_client()
        prompt = _build_proactive_prompt(score, period)
        messages = [{"role": "user", "content": "（主动给用户发一条消息。如果话题需要具体内容，可以先搜索一下再聊。）"}]

        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=prompt,
            messages=messages,
            tools=PROACTIVE_TOOL_DEFINITIONS,
        )

        # Handle tool use loop (max 3 rounds to avoid infinite loops)
        rounds = 0
        while response.stop_reason == "tool_use" and rounds < 3:
            rounds += 1
            tool_results = []
            reason_hint = collect_reason_hint(response.content)
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(
                        block.name,
                        block.input,
                        session_id=OWNER_SESSION,
                        reason_hint=reason_hint or None,
                    )
                    if isinstance(result, str) and len(result) > 2000:
                        result = result[:2000] + "...(截断)"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result if isinstance(result, str) else "OK",
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            response = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=prompt,
                messages=messages,
                tools=PROACTIVE_TOOL_DEFINITIONS,
            )

        text = join_text_blocks(response.content).strip()

        if text:
            save_message("assistant", text, OWNER_SESSION, source="proactive")
        return text
    except Exception as e:
        print(f"[pupu] proactive message generation failed: {e}")
        return None


async def proactive_loop(send_func):
    """Main proactive messaging loop. send_func(text) should send a private message to owner."""
    print("[pupu] proactive messaging started")
    try:
        while True:
            score = get_familiarity(OWNER_SESSION)
            freq = get_proactive_freq(score)

            if freq is None:
                await asyncio.sleep(300)
                continue

            interval = random.uniform(freq["min_interval"], freq["max_interval"]) * 60
            await asyncio.sleep(interval)

            if _is_quiet_hours():
                continue

            if _had_recent_chat_within(60):
                continue

            period = _get_current_period()
            if period is None:
                continue

            score = get_familiarity(OWNER_SESSION)
            if score < PROACTIVE_THRESHOLD:
                continue

            idle_minutes = _minutes_since_last_chat()
            should_send = await asyncio.to_thread(
                _model_should_proactively_reach_out,
                score,
                period,
                idle_minutes,
            )
            if not should_send:
                continue

            text = await asyncio.to_thread(generate_proactive_message, score, period)
            if text:
                try:
                    await send_func(text)
                    print(f"[pupu] proactive >>> {text[:80]}")
                except Exception as e:
                    print(f"[pupu] proactive send failed: {e}")
    except asyncio.CancelledError:
        print("[pupu] proactive messaging stopped")
