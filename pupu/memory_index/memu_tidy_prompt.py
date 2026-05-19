"""Prompt template for memU tidy judge."""

MEMU_TIDY_JUDGE_PROMPT = """你在帮一个长期陪伴型聊天体整理 memU 长期记忆。

任务：
- 只判断哪些条目应该删除。
- 只处理 user_fact / self_fact / important_event。
- 不要删除 summary。
- 不要改写、合并、拆分或新增条目。
- 只删除高置信的垃圾项；拿不准就保留。

删除标准：
- 明显重复，或者和更稳定、更完整的一条重复。
- 明显临时、一次性、过期、无信息、低价值。
- 对事实类：临时状态、当时情境、短期安排通常可删；稳定偏好、身份、长期习惯通常保留。
- 对重要事件类：未来事件、生日、纪念日、已安排任务、长期承诺保留；过期一次性事件、失效提醒可删。

输出 JSON，且只输出 JSON：
{
  "drop_ids": ["item_id_1", "item_id_2"],
  "notes": "一句简短说明",
  "reason_by_id": {
    "item_id_1": "重复",
    "item_id_2": "过期"
  }
}

要求：
- drop_ids 只能来自输入 items 的 id。
- 如果一个条目不该删，就不要放进 drop_ids。
- reason_by_id 只需要写被删条目，值用简短中文词组。
- 如果没有任何要删的条目，返回空数组和空字符串。
"""
