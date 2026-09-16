"""Prompt assembly helpers for persona-aware chat."""

from ..familiarity import score_to_level
from ..event_thread_context import format_event_threads_section
from ..storage.facts import group_person_facts_for_display
from .core import get_core_persona, get_pupu_name
from .familiarity_prompts import FAMILIARITY_PROMPTS


def _replace_default_character_name(text: object, character_name: str) -> str:
    value = str(text or "")
    if character_name and character_name != "仆仆":
        value = value.replace("仆仆", character_name)
    return value


def _format_person_facts(facts: list[dict], character_name: str) -> str:
    lines: list[str] = []
    for label, rows in group_person_facts_for_display(facts or []):
        label = _replace_default_character_name(label, character_name)
        lines.append(f"{label}:")
        for row in rows:
            key = _replace_default_character_name(row.get("fact_key"), character_name)
            value = _replace_default_character_name(row.get("fact_value"), character_name)
            if key and value:
                lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _format_recalled_memories(memories: list[dict], character_name: str) -> str:
    lines = []
    for item in memories:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- {text}")
    return "\n".join(lines)


def build_system_prompt(
    familiarity_score: int,
    summaries: list[dict] = None,
    person_facts: list[dict] = None,
    event_threads: list[dict] = None,
    reply_speed_hint: str = None,
    recalled_memories: list[dict] = None,
    include_familiarity_prompt: bool = True,
    group_people_context: str = "",
) -> str:
    level = score_to_level(familiarity_score)
    character_name = get_pupu_name()
    core_persona = _replace_default_character_name(get_core_persona(), character_name)
    prompt = (
        f"## 当前身份\n"
        f"你就是{character_name}。用户现在是在和{character_name}说话，"
        f"不要把自己说成其他名字或其他角色。\n\n"
        + core_persona
    )
    if include_familiarity_prompt:
        prompt += "\n" + FAMILIARITY_PROMPTS[level]

    group_people_context = str(group_people_context or "").strip()
    if group_people_context:
        prompt += "\n\n## 当前群聊人物\n" + group_people_context

    person_facts_section = _format_person_facts(person_facts or [], character_name)
    if person_facts_section:
        prompt += "\n\n## Long-term Facts By Person\n" + person_facts_section
        prompt += "\nThese facts are scoped to people or relationships. Use them naturally, but do not repeat them mechanically."

    if summaries:
        prompt += "\n\n## 之前聊过\n"
        prompt += "\n".join(
            f"- [用户 / {character_name}] "
            f"{str(item.get('summary') or '').strip()}"
            for item in summaries
            if str(item.get("summary") or "").strip()
        )

    event_threads_section = format_event_threads_section(
        event_threads,
        heading=f"## {character_name}记得并在意的事",
        subject_label=f"用户 / {character_name}",
        character_name=character_name,
    )
    if event_threads_section:
        prompt += "\n\n" + event_threads_section

    recalled_section = _format_recalled_memories(recalled_memories or [], character_name)
    if recalled_section:
        prompt += "\n\n## 本轮自然想起的事\n" + recalled_section
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
