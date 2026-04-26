"""Prompt assembly helpers for persona-aware chat."""

from ..familiarity import score_to_level
from .core import CORE_PERSONA
from .familiarity_prompts import FAMILIARITY_PROMPTS


def build_system_prompt(
    familiarity_score: int,
    event_log: list[dict] = None,
    user_facts: dict[str, str] = None,
    summaries: list[dict] = None,
    self_facts: dict[str, str] = None,
    reply_speed_hint: str = None,
) -> str:
    level = score_to_level(familiarity_score)
    prompt = CORE_PERSONA + "\n" + FAMILIARITY_PROMPTS[level]

    if self_facts:
        prompt += "\n\n## 关于你自己（你之前说过的设定）\n"
        for key, value in self_facts.items():
            prompt += f"- {key}：{value}\n"
        prompt += "\n这些是你之前告诉对方的，要保持一致，不要自相矛盾。"
        prompt += "\n聊天时可以自然地从自己的爱好出发找话题、接话、延伸。"

    if user_facts:
        prompt += "\n\n## 你对这个人的了解\n"
        for key, value in user_facts.items():
            prompt += f"- {key}：{value}\n"
        if level in ("认识", "熟悉"):
            prompt += "\n你知道这些但不会主动提，除非对方先聊到相关内容。"
        else:
            prompt += "\n你已经挺熟了，可以主动用这些信息找话题、接话、关心对方。"
            prompt += "\n比如知道对方在哪就可以聊当地天气，知道对方喜欢什么就可以分享相关东西。"
            prompt += "\n不用每次都用，自然就好，像真的记得朋友说过的事一样。"

    if summaries:
        prompt += "\n\n## 之前聊过的内容（摘要）\n"
        for summary in summaries:
            prompt += f"- {summary['summary']}\n"

    if event_log:
        prompt += "\n\n## 你们一起经历过的事（你的记忆）\n"
        for event in event_log[-10:]:
            prompt += f"- {event['description']}\n"
        prompt += "\n用这些记忆来让对话更自然，偶尔可以提到之前的事。"

    if reply_speed_hint:
        prompt += (
            f"\n\n## 用户回复速度\n{reply_speed_hint}\n"
            "根据这个信息自然地调整你的反应，不要直接说出来。"
        )

    prompt += (
        "\n\n## 工具使用硬规则（定时任务）\n"
        "- 用户明确表达提醒、记得、到时候叫我、生日祝福、定时等意图时，优先调用对应的日程工具。\n"
        "- 没拿到工具返回的成功结果前，不要说“已设置”“已创建”“我记住了到时提醒你”。\n"
        "- 若缺少关键信息，比如日期或时间，先追问，不要自己编一个时间。\n"
        "- repeat 支持 once、daily、weekly、monthly、yearly、interval；用户说每年、每月、每周、每天时，可直接按对应周期创建。\n"
    )

    return prompt
