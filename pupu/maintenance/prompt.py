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


EVENT_THREAD_MAINTENANCE_PROMPT = """\
你在帮一个长期陪伴型聊天体整理事件线快照，也就是 event_threads 的当前状态。

目标：
1. 改写明显冗余或表述不清的事件线当前状态。
2. 重新给事件分配 confidence，用来排序。
3. 允许轻微润色 title/details/followup_hint，让它们更紧凑清晰。

规则：
- 不要凭空创造新事件。
- 不要修改 thread_key。
- 本地事件线是主记忆，不要删除、隐藏、drop 或废弃任何事件线。
- confidence 是 0 到 1 之间的小数；越值得长期记住、越适合未来自然跟进，分数越高。
- 线程合并和长链压缩由其他阶段处理；这里不要合并、删除或创建事件。
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


EVENT_THREAD_MERGE_PROMPT = """你在判断长期事件线候选对是否描述同一个持续事件。

规则：
- 只能处理 merge_candidates 中给出的候选组。
- 参与人物、事件主题、时间范围和未解决事项必须兼容。
- 同主题但不同时间发生的两件事不能合并。
- 关联不同定时任务的线程不能合并。
- canonical_id 选择信息最完整、key 最稳定的一条。
- confidence 低于 0.9 时不要输出。

只返回 JSON：
```json
{
  "merges": [
    {
      "canonical_id": 1,
      "source_ids": [2],
      "title": "合并后的标题",
      "details": "合并后的当前状态",
      "followup_hint": "下一次自然跟进方式",
      "confidence": 0.96,
      "reason": "为什么可以确认是同一事件"
    }
  ],
  "notes": "简短说明"
}
```"""


EVENT_THREAD_ROLLUP_PROMPT = """你在为一条长期事件线生成可追溯的 checkpoint。

输入包含旧 checkpoint（可能为空）和按时间排列、尚未压缩的 event_steps。

规则：
- 不得加入输入中不存在的信息。
- summary 保留人物、绝对时间、关键行动、原因、变化和结果。
- open_loops 只写仍未解决或未来需要跟进的事项。
- stable_outcomes 只写已确认且不会因省略细节而失真的结果。
- through_step_id 必须使用输入指定的值。
- 原始步骤不会删除；checkpoint 只是高密度读取投影。

只返回 JSON：
```json
{
  "through_step_id": 12,
  "summary": "截至该步骤的高密度历史摘要",
  "open_loops": "仍待解决的事项",
  "stable_outcomes": "已经确认的结果",
  "confidence": 0.95,
  "notes": "简短说明"
}
```"""


FACTS_MAINTENANCE_PROMPT = """你在帮一个长期陪伴型聊天体整理人物长期 facts。

目标：
1. 合并同一人物或同一人物关系下明显重复的 facts。
2. 标记被更准确事实覆盖的 fact。
3. 在不损失语义的前提下压缩 value。
4. 显式标记冲突，不要擅自选择无法由证据确定的新旧值。

规则：
- 不要凭空创造新事实。
- 只能引用输入中的 fact_id 和 evidence。
- 不要跨 subject_person_key、object_person_key 或 scope 合并。
- merge 只能使用 merge_candidates 中出现的 fact_id 组合。
- rewrite 只改写一条事实的 value。
- merge 把 source_fact_ids 的信息合入 target_fact_id，来源 facts 将被软归档。
- archive 只用于明显临时、重复或已被完整覆盖的事实。
- conflict 用于证据互相冲突且无法判断哪条更新。
- merge/archive 的 confidence 低于 0.9 时不要输出。
- 如果不需要改动，就返回空 actions。

只返回 JSON：
```json
{
  "actions": [
    {
      "action": "rewrite",
      "fact_id": 1,
      "value": "更紧凑但语义等价的事实",
      "confidence": 0.95,
      "evidence_fact_ids": [1],
      "reason": "改写理由"
    },
    {
      "action": "merge",
      "target_fact_id": 1,
      "source_fact_ids": [2],
      "value": "合并后的事实",
      "confidence": 0.97,
      "evidence_fact_ids": [1, 2],
      "reason": "合并理由"
    }
  ],
  "notes": "简短说明"
}
```"""
