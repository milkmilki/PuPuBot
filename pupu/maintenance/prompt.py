"""Prompt templates for model-assisted maintenance."""

SUMMARY_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理长期摘要。

目标：
1. 找出明显重复、冗余、可被合并的 summaries。
2. 如果适合，把多条摘要合成一条更紧凑的新摘要。
3. 不要触碰人物 facts、实例设定、事件线或 tasks。

规则：
- 只处理 summaries。
- 只有在两条及以上摘要明显重复时，才输出 drop_summary_ids。
- 只有在确实能合并得更紧凑时，才输出 merged_summary。
- 如果不需要改动，就返回空数组和空字符串。

只返回 JSON：
```json
{
  "drop_summary_ids": [1, 2],
  "merged_summary": "合并后的摘要",
  "notes": "简短说明"
}
```"""


EVENT_THREAD_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理事件线快照，也就是 event_threads 的当前状态。

目标：
1. 合并和改写明显重复、冗余或表述不清的事件线当前状态。
2. 重新给事件分配 confidence，用来排序。
3. 允许轻微润色 title/details/followup_hint，让它们更紧凑清晰。

规则：
- 不要凭空创造新事件。
- 不要修改 thread_key。
- 本地事件线是主记忆，不要删除、隐藏、drop 或废弃任何事件线。
- confidence 是 0 到 1 之间的小数；越值得长期记住、越适合未来自然跟进，分数越高。
- 如果某条事件只是在当前这一小批里语义重复，请通过 updates 改写标题、details、followup_hint 或 confidence，不要删除。
- 如果不需要改动，就返回空 updates 或只更新你想改的项。

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


FACTS_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理人物长期 facts。

目标：
1. 合并明显重复的 owner_facts 和 instance_facts。
2. 删除明显冗余、过时或被更准确事实覆盖的 key。
3. 轻微改写 value，让事实更紧凑清晰。

规则：
- owner_facts 只记录当前对话对象的稳定事实。
- instance_facts 只记录当前实例自己的稳定设定。
- 不要凭空创造新事实。
- 不要把 owner_facts 和 instance_facts 互相搬家。
- 只处理明显重复或明显冲突的项；不确定就不要改。
- updates 只能使用输入里已有的 key。
- 如果要合并两条事实，更新更清晰、更稳定的那条 key，再删除重复 key。
- 如果不需要改动，就返回空对象和空数组。

只返回 JSON：
```json
{
  "owner_updates": {
    "保留的对象事实key": "整理后的对象事实"
  },
  "owner_delete_keys": ["要删除的对象事实key"],
  "instance_updates": {
    "保留的实例事实key": "整理后的实例事实"
  },
  "instance_delete_keys": ["要删除的实例事实key"],
  "notes": "简短说明"
}
```"""
