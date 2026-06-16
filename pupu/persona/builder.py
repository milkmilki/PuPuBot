"""Prompt assembly helpers for persona-aware chat."""

from ..familiarity import score_to_level
from ..important_event_context import format_important_events_section
from .core import get_core_persona, get_pupu_name
from .familiarity_prompts import FAMILIARITY_PROMPTS


def _replace_default_character_name(text: object, character_name: str) -> str:
    value = str(text or "")
    if character_name and character_name != "仆仆":
        value = value.replace("仆仆", character_name)
    return value


def _format_facts(facts: dict[str, str], subject: str, character_name: str) -> str:
    return "\n".join(
        f"- {subject} | {_replace_default_character_name(key, character_name)}: "
        f"{_replace_default_character_name(value, character_name)}"
        for key, value in facts.items()
    )


def _memory_subject(kind: str, character_name: str) -> str:
    if kind == "user_fact":
        return "用户"
    if kind == "self_fact":
        return character_name
    if kind in {"summary", "important_event"}:
        return f"用户 / {character_name}"
    return "相关记忆"


def _format_recalled_memories(memories: list[dict], character_name: str) -> str:
    lines = []
    for item in memories:
        text = _replace_default_character_name(item.get("text") or "", character_name).strip()
        if not text:
            continue
        kind = str(item.get("kind") or "memory").strip()
        subject = _memory_subject(kind, character_name)
        lines.append(f"- [{kind} | {subject}] {text}")
    return "\n".join(lines)


def build_system_prompt(
    familiarity_score: int,
    event_log: list[dict] = None,
    user_facts: dict[str, str] = None,
    summaries: list[dict] = None,
    self_facts: dict[str, str] = None,
    important_events: list[dict] = None,
    reply_speed_hint: str = None,
    recalled_memories: list[dict] = None,
) -> str:
    level = score_to_level(familiarity_score)
    character_name = get_pupu_name()
    prompt = get_core_persona() + "\n" + FAMILIARITY_PROMPTS[level]

    if self_facts:
        prompt += f"\n\n## {character_name}自己的设定\n" + _format_facts(
            self_facts,
            character_name,
            character_name,
        )
        prompt += "\n保持一致，不要自相矛盾。"

    if user_facts:
        prompt += "\n\n## 关于用户的长期记忆\n" + _format_facts(
            user_facts,
            "用户",
            character_name,
        )
        if level in ("认识", "熟悉"):
            prompt += "\n除非对方先提，不主动拿这些信息起话头。"
        else:
            prompt += "\n可自然引用，但不要每次都提。"

    if summaries:
        prompt += "\n\n## 之前聊过\n"
        prompt += "\n".join(
            f"- [用户 / {character_name}] "
            f"{_replace_default_character_name(item['summary'], character_name)}"
            for item in summaries
        )

    important_events_section = format_important_events_section(
        important_events,
        heading=f"## {character_name}记得并在意的事",
        subject_label=f"用户 / {character_name}",
        character_name=character_name,
    )
    if important_events_section:
        prompt += "\n\n" + important_events_section

    recalled_section = _format_recalled_memories(recalled_memories or [], character_name)
    if recalled_section:
        prompt += "\n\n## 本轮自然想起的记忆\n" + recalled_section
        prompt += "\n这些是当前对话相关的联想线索，可以自然使用，但不要生硬复述。"

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
