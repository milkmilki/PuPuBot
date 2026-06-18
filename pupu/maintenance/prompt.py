"""Prompt templates for model-assisted maintenance."""

SUMMARY_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理长期摘要。

目标：
1. 找出明显重复、冗余、可被合并的 summaries
2. 如果适合，把多条摘要合成一条更紧凑的新摘要
3. 不要碰用户 facts、自我设定、事件线、tasks

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


EVENT_THREAD_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理事件线快照（event_threads 的当前状态）。

目标：
1. 合并和改写明显重复、冗余或表述不清的事件线当前状态
2. 重新给事件分配 confidence，用来排序
3. 允许轻微润色 title/details/followup_hint，让它们更紧凑清晰

规则：
- 不要凭空创造新事件
- 不要修改 thread_key
- 本地事件线是主记忆，不要删除、隐藏、drop 或废弃任何事件线
- confidence 用 0 到 1 之间的小数；越值得长期记住、越适合未来自然跟进，分数越高
- 如果某条事件只是当前这一小批里语义重复，请通过 updates 改写标题、details、followup_hint 或 confidence，不要删除
- 如果不需要改动，就返回空 updates 或只更新你想改的项

只返回 JSON：
```json
{
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


FACTS_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理长期 facts。

目标：
1. 合并明显重复的 user_facts 和 self_facts
2. 删除明显冗余、过时、或被更准确事实覆盖的 key
3. 轻微改写 value，让事实更紧凑清晰

规则：
- user_facts 只记录用户的稳定事实；self_facts 只记录仆仆自己的稳定设定
- 不要凭空创造新事实
- 不要把 user_facts 和 self_facts 互相搬家
- 只处理明显重复或明显冲突的项；不确定就不要改
- updates 只能使用输入里已有的 key
- 如果要合并两条事实，请更新更清楚、更稳定的那条 key，再删除重复 key
- 如果不需要改动，就返回空对象和空数组

只返回 JSON：
```json
{
  "user_updates": {
    "保留的用户事实key": "整理后的用户事实"
  },
  "user_delete_keys": ["要删除的用户事实key"],
  "self_updates": {
    "保留的自我设定key": "整理后的自我设定"
  },
  "self_delete_keys": ["要删除的自我设定key"],
  "notes": "简短说明"
}
```"""
