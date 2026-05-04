"""Prompt assembly helpers for persona-aware chat."""

from ..familiarity import score_to_level
from ..important_event_context import format_important_events_section
from .core import get_core_persona
from .familiarity_prompts import FAMILIARITY_PROMPTS


def _format_facts(facts: dict[str, str]) -> str:
    return "\n".join(f"- {key}: {value}" for key, value in facts.items())


def build_system_prompt(
    familiarity_score: int,
    event_log: list[dict] = None,
    user_facts: dict[str, str] = None,
    summaries: list[dict] = None,
    self_facts: dict[str, str] = None,
    important_events: list[dict] = None,
    reply_speed_hint: str = None,
) -> str:
    level = score_to_level(familiarity_score)
    prompt = get_core_persona() + "\n" + FAMILIARITY_PROMPTS[level]

    if self_facts:
        prompt += "\n\n## 你自己的设定\n" + _format_facts(self_facts)
        prompt += "\n保持一致，不要自相矛盾。"

    if user_facts:
        prompt += "\n\n## 你对对方的了解\n" + _format_facts(user_facts)
        if level in ("认识", "熟悉"):
            prompt += "\n除非对方先提，不主动拿这些信息起话头。"
        else:
            prompt += "\n可自然引用，但不要每次都提。"

    if summaries:
        prompt += "\n\n## 之前聊过\n"
        prompt += "\n".join(f"- {item['summary']}" for item in summaries)

    important_events_section = format_important_events_section(important_events)
    if important_events_section:
        prompt += "\n\n" + important_events_section

    if reply_speed_hint:
        prompt += f"\n\n## 回复节奏\n{reply_speed_hint}\n自然调整，不要直接说出来。"

    prompt += (
        "\n\n## 定时任务规则\n"
        "- 用户明确表达提醒、记得、到时候叫我、生日祝福、定时等意图时，优先调用日程工具。\n"
        "- 没拿到工具成功结果前，不要说“已设置”“已创建”“我记住了到时提醒你”。\n"
        "- 缺日期或时间就先追问，不要自己编。\n"
        "- repeat 只用 once、daily、weekly、monthly、yearly、interval。"
    )
    return prompt
