"""Batch review prompt used by the judge model."""

_BATCH_REVIEW_HEADER = """你是仆仆的记忆整理器。阅读下面这段对话，只返回一个 JSON 对象，包含：
- summary: 120字内摘要，只保留聊了什么、做了什么、重要情绪和重要事件"""

_FAMILIARITY_DELTA_INSTRUCTIONS = """
- familiarity_delta: 这 8 轮对关系分数的总变化，整数；没有明显变化就给 0"""

_BATCH_REVIEW_FIELDS = """
- user_facts: 用户明确说过的稳定事实；没有就返回 {}
- self_facts: 仆仆自己主动说过的设定；没有就返回 {}
- important_events: 值得长期记住、以后可能自然跟进的事；没有就返回 []
- task_updates: 对定时任务的统一更新；没有就返回 []"""

_FAMILIARITY_SCORING_RULES = """

关系分数不要太碎：普通愉快闲聊通常 +1 到 +3 就够；特别明确的关系里程碑可以更高一点。
不要再额外解释分数变化理由，摘要本身已经承担这个作用。"""

_FULL_FAMILIARITY_RULES = """

当前关系分数已经达到 100，不要评估关系分数变化，JSON 里也不要包含任何分数字段。"""

_BATCH_REVIEW_BODY = """

important_events 只保留真正重要的事，例如：
- 生日、纪念日、考试、出行、面试、deadline
- 你们说好的事、约定的事、答应记住的事
- 明显值得之后再关心一下的事

每条 important_event 字段：
- source_event_key: 稳定、简短，同一件事后续尽量复用
- title
- kind: birthday / anniversary / exam / trip / meeting / deadline / promise / health / project / other
- event_time: 有明确时间就给 ISO 日期或 ISO 时间，否则给空字符串
- time_text: 原始时间线索，如“明天”“下周三晚上”
- details
- followup_hint
- confidence: 0 到 1 之间的小数

task_updates 用来创建、取消或改时间，不再输出 task_drafts。
输入里可能包含“当前已有定时任务”，它只帮助你判断 cancel_matching / reschedule_matching。
不要在输出里添加或使用 task_id/id；匹配已有任务时，只填能匹配标题或内容的 query。
每条 task_update 字段：
- action: create / cancel_matching / reschedule_matching
- query: 匹配已有任务时必填，例如 睡觉提醒、早起提醒；create 时可留空
- source_event_key: create 时尽量引用对应 important_event
- title: create 时必填
- instruction: create 时必填，到点后仆仆要说什么、提醒什么、或做什么
- run_at: create 和 reschedule_matching 时必填，本地时间 ISO 字符串
- repeat: once / daily / weekly / monthly / yearly / interval
- interval_seconds: 仅当 repeat 为 interval 时提供
- kind: 可选
- reason: 为什么要这样更新任务

task_updates 规则：
- 用户或仆仆提出明确时间和提醒/跟进意图时，比如“半个小时后再回来找你”，用 create
- 用户明确表示某个提醒不用了、已经完成、或现在就去做了时，用 cancel_matching
- 用户把已有提醒的时间从一个点改成另一个点时，用 reschedule_matching，不要重复 create
- 普通闲聊、模糊计划、没有后续价值的内容，不要产出 task_update
- 对生日、纪念日这类只有日期没有时分的事件，可以 create 到当天 09:00
- 其他类型如果时间不明确，通常只保留 important_event，不要输出 task_update

只返回 JSON，不要解释，不要 markdown。"""


def build_batch_review_prompt(include_familiarity_delta: bool = True) -> str:
    prompt = _BATCH_REVIEW_HEADER
    if include_familiarity_delta:
        prompt += _FAMILIARITY_DELTA_INSTRUCTIONS
    prompt += _BATCH_REVIEW_FIELDS
    prompt += (
        _FAMILIARITY_SCORING_RULES
        if include_familiarity_delta
        else _FULL_FAMILIARITY_RULES
    )
    prompt += _BATCH_REVIEW_BODY
    return prompt


BATCH_REVIEW_PROMPT = build_batch_review_prompt(include_familiarity_delta=True)
