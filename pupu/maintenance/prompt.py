"""Prompt templates for model-assisted maintenance."""

SUMMARY_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理长期摘要。

目标：
1. 找出明显重复、冗余、可被合并的 summaries
2. 如果适合，把多条摘要合成一条更紧凑的新摘要
3. 不要碰用户 facts、自我设定、important_events、tasks

规则：
- 只处理 summaries
- 只有在两条及以上摘要明显重叠时，才输出 drop_summary_ids
- 只有在确实能合并得更紧凑时，才输出 merged_summary
- 如果不需要改动，就返回空数组和空字符串

只返回 JSON：
```json
{
  "drop_summary_ids": [1, 2],
  "merged_summary": "合并后的摘要",
  "notes": "简短说明"
}
```"""


IMPORTANT_EVENT_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理 important_events。

目标：
1. 删除明显重复、价值很低、或已经过时且不值得长期记住的 important_events
2. 重新给保留事件分配 confidence，用来排序
3. 允许轻微润色 title/details/followup_hint，让它们更紧凑清晰

规则：
- 不要凭空创造新事件
- 不要修改 source_event_key
- 不要删除 linked_task_id 不为空的事件
- 不要删除 status 为 scheduled 的事件
- confidence 用 0 到 1 之间的小数；越值得长期记住、越适合未来自然跟进，分数越高
- 如果某条事件只是当前这一小批里语义重复，请保留更完整、更清楚的一条，删除其余重复项
- 如果不需要改动，就返回空 drop_ids，并把 updates 原样给回去或只更新你想改的项

只返回 JSON：
```json
{
  "drop_ids": [3],
  "updates": [
    {
      "id": 1,
      "title": "整理后的标题",
      "kind": "promise",
      "event_time": "2026-04-26",
      "time_text": "今天",
      "details": "更紧凑的描述",
      "followup_hint": "更自然的跟进方式",
      "confidence": 0.92
    }
  ],
  "notes": "简短说明"
}
```"""
