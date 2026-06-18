"""Prompt template for memU tidy judge."""

MEMU_TIDY_JUDGE_PROMPT = """你在帮一个长期陪伴型聊天体整理 memU 长期记忆缓存。

执行边界：
- 输入 items 全部是 memU 缓存条目，不是 SQLite 本地源记录。
- 你只能选择要删除的 memU item_id；程序只会执行 memU delete_memory_item。
- 不存在删除、隐藏、drop、废弃或改写本地 facts、本地事件线、聊天记录的操作。

任务：
- 只处理 person_fact / event_thread。
- 不要删除 summary。
- 不要改写、合并、拆分或新增条目。
- 只删除高置信的垃圾缓存项；拿不准就保留。
- event_thread 是本地事件线的索引副本，只能删除明显重复或垃圾的 memU 副本；不要因为事件过期就要求隐藏、drop 或废弃本地事件线。

删除标准：
- 明显重复，或者和更稳定、更完整的一条重复。
- 明显临时、一次性、过期、无信息、低价值。
- 对 person_fact 缓存类：临时状态、当时情境、短期安排通常可删；稳定偏好、身份、长期习惯通常保留。
- 对 event_thread 缓存类：未来事件、生日、纪念日、已安排任务、长期承诺保留；只有明显重复、低价值或垃圾缓存副本才可删。

输出 JSON，且只输出 JSON。不要输出任何其他操作字段：
{
  "drop_ids": ["item_id_1", "item_id_2"],
  "notes": "一句简短说明",
  "reason_by_id": {
    "item_id_1": "重复",
    "item_id_2": "低价值"
  }
}

要求：
- drop_ids 只能来自输入 items 的 id。
- 如果一个条目不该删，就不要放进 drop_ids。
- reason_by_id 只需要写被删条目，值用简短中文词组。
- 如果没有任何要删的条目，返回空数组和空字符串。
"""
