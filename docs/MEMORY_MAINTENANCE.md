# PuPuBot 每日记忆整合系统

本文说明 PuPuBot 改造后的长期记忆整合流程。系统目标不是定期删除历史，而是在保留证据和可审计性的前提下，持续降低可用记忆的冗余度。

完整流程图：[打开每日记忆整合流程图](assets/memory-maintenance-flow.html)

## 1. 设计目标

系统遵守四条核心约束：

1. **SQLite 是唯一事实源。** `semantic_cards` 只是可删除、可重建的召回缓存。
2. **原始证据不因整理丢失。** Facts 保留 evidence；长事件线保留全部 `event_steps`；旧消息先归档再从热表移除。
3. **模型只负责受约束的判断。** 候选集合、作用域、置信度、并发版本和自动破坏性动作门禁都由确定性代码控制。
4. **网络调用不持有数据库写锁。** 模型规划完成后，系统才执行版本校验和短事务写入。

系统将记忆分成四层：

| 层级 | 主要数据 | 作用 |
|---|---|---|
| 原始证据层 | `messages`、`archived_messages`、`event_steps`、`fact_evidence` | 保存事实和事件的来源 |
| 当前记忆层 | `person_facts`、`event_threads`、`summaries` | 提供当前、结构化的记忆状态 |
| 整理审计层 | `memory_mutations`、`event_thread_rollups`、`event_thread_aliases`、`maintenance_runs` | 记录变更、checkpoint、旧键重定向和运行结果 |
| 语义投影层 | `semantic_cards`、`semantic_sync_log` | 提供 embedding 召回，可从事实源重建 |

## 2. 入口与触发

### 2.1 自动运行

Actor 和 CLI 的后台循环按以下顺序调用：

1. `maybe_run_daily_backup()`
2. `maybe_run_daily_maintenance()`

当 `runtime.memory_maintenance_enabled` 为 `true`，且当前时间不早于 `runtime.memory_maintenance_hour` 时，系统检查当天是否已有成功的 `auto` maintenance 记录。没有成功记录才会执行，因此一天最多成功执行一次；失败后当天仍可重试。

默认配置：

```yaml
runtime:
  memory_maintenance_enabled: true
  memory_maintenance_hour: 3
  backup_dir: ""
```

自动备份与 maintenance 是两个顺序调用的独立操作。备份完成后 maintenance 失败，不会破坏当天的备份；maintenance 本身也不会把两者包装成一个事务。

### 2.2 手动运行

```text
/maintain check
/maintain
/maintain apply
```

- `check`：运行模型规划和语义索引一致性检查，但不修改源记忆。
- `apply`：应用允许执行的整理动作，再同步语义索引。
- `/maintain` 等价于 `/maintain apply`。

手动 `/maintain` 不会隐式创建数据库备份。需要操作前快照时，应先使用现有备份入口。

`/tidy` 的职责不同：

```text
/tidy check
/tidy apply
/tidy rebuild
```

`/tidy` 只维护 `semantic_cards` 与 SQLite 事实源的一致性，不归并 facts、summaries 或 event threads。

## 3. 端到端流程

### 3.1 单进程互斥

`run_memory_maintenance()` 首先尝试获取进程内非阻塞锁。已有 maintenance 运行时，新请求立即返回 busy，不会并发执行第二套整理流程。

该锁只覆盖当前 Python 进程。部署时应确保同一实例数据库只有一个 Actor/CLI 维护者，不能依赖该锁协调多个独立进程。

### 3.2 初始化与基线指标

系统初始化数据库迁移，枚举所有具有消息、摘要、任务、事件线或事实来源的 session，并读取整理前指标：

- 活跃 facts 数量
- 活跃事件线数量
- 事件步骤与 rollup 数量
- Fact evidence 覆盖率
- 当前投影字符数
- 原始事件证据字符数
- 证据/投影压缩比
- 已归档消息数量

共享 facts 可能出现在多个 session 快照中，因此运行期用 `processed_fact_ids` 保证每条 fact 每轮只处理一次。

### 3.3 确定性预整理

`apply` 模式先执行不依赖模型的清理：

- 去重 summaries、旧好感度 events 和 scheduled tasks。
- 将已总结且超过热窗口的聊天消息复制到 `archived_messages`，再从 `messages` 删除。
- 将过期的 scheduled/proactive 内部消息先归档，再从热表删除。
- 删除超过保留期的已禁用定时任务。

这一步完成后立即提交，避免后续模型网络调用持有 SQLite 写事务。

`check` 模式跳过上述写操作。

### 3.4 构建会话快照

每个 session 的维护快照包含：

- summaries
- 当前人物 facts、实例 facts 和关系 facts
- 每条 fact 最近的 evidence 及原始消息摘录
- scheduled tasks
- event threads
- 尚未被 checkpoint 覆盖的 event steps

Fact 原文摘录会同时查询 `messages` 和 `archived_messages`。消息从热表迁出后，模型仍能看到对应证据。

输入规模受确定性代码限制：

- Facts 每批最多 30 条，每条只携带最近 8 条 evidence，原文摘录最多 600 字符。
- Event 当前状态每批最多 12 条。
- Event rollup 每次最多发送 24 条旧步骤，并保留最近 4 条不压缩。
- Event merge 只发送确定性代码筛出的最多 40 组候选。
- Persona seed 且没有对话证据修订的 facts 不进入模型整理。

### 3.5 模型规划

每个 session 依次执行三类规划：

1. Summary 合并
2. Event thread 整理、合并和 rollup
3. Fact 改写、合并、归档和冲突识别

Event 和 Fact 模块都先完成该阶段的全部模型调用，再进入数据库应用循环。模型输出必须经过结构化解析、ID 白名单、候选集合和置信度校验，不能直接执行任意数据库变更。

模型输入以 JSON 写入 user message，任务约束和返回 schema 写入 system prompt。模型只能返回 JSON object；解析失败时，当前任务不执行任何动作，并写入：

```text
instances/<instance>/data/logs/maintenance-model-errors-YYYYMMDD.jsonl
```

每条错误记录包含时间、任务名、实际 Provider、异常类型、异常信息、raw response、原始字符数和 SHA-256。raw 正文硬限制为 1,000,000 字符，日志文件权限设置为 `0600`，按天保留最近 14 个文件。日志写入失败不会覆盖原始解析异常。

各阶段应用后分别提交。某个 session 的模型阶段失败时，当前未提交事务会回滚、错误写入报告，后续 session 继续处理；此前已经提交的阶段不会反向撤销。因此 maintenance 是分阶段可恢复流程，不是全库单事务。

### 3.6 并发与动作校验

模型规划完成后，系统重新读取将要修改的记录：

- Fact 比较 `fact_value`、`status` 和 `updated_at`。
- Event thread 比较 `updated_at`、`status` 和 `current_step_id`。

任一字段与快照不一致，说明规划期间出现了新写入，该动作会被跳过，避免旧计划覆盖新对话产生的状态。

自动和手动运行采用相同策略：Fact merge/archive 和 Event thread merge 在当前维护轮次通过候选范围、置信度和并发版本校验后立即执行，不等待后续轮次确认。

### 3.7 短事务应用

通过校验的动作写入 SQLite，并在 `memory_mutations` 保存：

- 变更实体和动作类型
- before / after JSON
- evidence
- 原因
- 置信度
- fingerprint
- `applied` 或 `conflict` 状态

模型网络请求与写事务分离，写锁只覆盖实际更新和审计记录。

### 3.8 语义同步与运行报告

源记忆整理完成后，系统调用 semantic tidy：

- 补齐缺失 card
- 删除孤儿或重复 card
- 刷新 source version 已变化的 card
- 将 event rollup 纳入事件投影
- 排除 inactive facts 和已合并的源事件线

最后重新计算信息密度指标，将成功或失败报告写入 `maintenance_runs`。自动触发是否“当天已完成”只认 `trigger='auto' AND status='success'`。

## 4. Summaries 整理

当同一 session 至少有两条 summary 时，模型可以选择一组 summary 合并为一条覆盖范围更大的摘要。系统只有在合并结果非空且来源至少两条时才应用：

- 新摘要继承来源集合最小的 `start_msg_id` 和最大的 `end_msg_id`。
- 来源 summary 行被删除。
- 来源内容和合并结果写入 `memory_mutations`。
- 对应原始聊天消息仍按归档策略保存在 `messages` 或 `archived_messages`。

Summary 合并与其他整理动作一样在当前维护轮次执行，并依赖完整 mutation 审计和自动运行前的数据库备份提供恢复边界。

## 5. Facts 整理

Facts 覆盖所有人物事实和关系事实。合并候选必须具有相同的作用域签名：

```text
(subject_person_key, object_person_key, scope)
```

模型只能在确定性候选集合内提出 merge，不能跨人物或跨关系作用域归并。

由 persona 配置播种、且没有对话证据修订的 seed facts 不进入模型整理，避免 maintenance 改写角色设定。

| 动作 | 最低置信度 | 数据结果 | 单次 apply |
|---|---:|---|---|
| `rewrite` | 0.8 | 更新目标 fact 的值，保留 evidence | 立即执行 |
| `merge` | 0.9 | 更新目标 fact；来源 fact 标记为 `superseded`；复制 evidence | 立即执行 |
| `archive` | 0.9 | 将 fact 标记为 `archived`，不物理删除 | 立即执行 |
| `conflict` | 无破坏性写入 | 保留原 fact，记录 conflict 审计 | 立即记录 |

读取 facts 时默认只返回 `status='active'`。旧事实仍可通过审计、inactive 查询和 evidence 追溯。

## 6. Event threads 整理

### 5.1 当前状态改写

模型可以规范化 title、kind、时间、details、followup hint 和 confidence。输出置信度必须至少为 0.8。

若结果与当前状态完全相同，系统不写入、不追加 system step，保证重复运行幂等。

### 5.2 重复线程合并

系统先生成确定性候选，再交给模型裁决。以下条件会阻止候选合并：

- 两边都有 `event_time` 且不相同
- 两边都有 `kind` 且不相同
- 两边关联了不同 scheduled task
- 两边有明确人物集合但没有交集
- 标题不同且文本相似度低于阈值

合并应用时：

- 保留 canonical thread。
- 把来源线程的 `event_steps` 和 `event_people` 迁移到 canonical thread。
- 兼容唯一的 scheduled task 关联。
- 将来源线程标记为 `merged`。
- 把旧 `thread_key` 写入 `event_thread_aliases`。
- 后续通过旧 key 查询时自动解析到 canonical thread。
- FTS 和 semantic projection 不再索引 merged source thread。

原始事件步骤不会因合并被删除。

### 5.3 长链 checkpoint

当某条事件线未压缩步骤超过 12 条时：

1. 保留最近 4 条步骤作为高分辨率上下文。
2. 每批最多选取 24 条更早步骤。
3. 模型生成 summary、stable outcomes 和 open loops。
4. 写入 `event_thread_rollups`。
5. 更新线程的 `compacted_through_step_id` 和 `rollup_summary`。

`event_steps` 原始行保持不变。后续维护快照只读取 checkpoint 之后的步骤，聊天上下文和语义投影使用“历史摘要 + 已确认结果 + 待跟进事项 + 最近步骤”。

## 7. 数据安全与恢复边界

| 风险 | 防护 |
|---|---|
| 模型凭空合并实体 | 候选集合、ID 白名单和作用域硬约束 |
| 模型规划覆盖新消息 | 应用前版本比较，发现变化即跳过 |
| 自动任务误归档或误合并 | 候选白名单、置信度阈值、并发版本检查、审计与每日备份 |
| 消息清理导致 evidence 丢失 | 先复制到 `archived_messages` 再删除热表记录 |
| 长事件线无限进入 prompt | checkpoint 只压缩投影，不删除原始步骤 |
| 旧事件 key 失效 | alias 重定向 canonical thread |
| 语义缓存污染事实源 | 单向从 SQLite 投影；缓存可全量重建 |
| 长时间模型调用锁库 | 模型规划与数据库短事务分离 |
| 模型返回不可解析内容 | 终止当前任务，并将 raw response 写入受限 JSONL 诊断日志 |

当前恢复方式是依据 `memory_mutations.before_json`、原始 evidence 和每日数据库备份进行人工或脚本化恢复。系统记录了恢复所需信息，但没有提供自动 undo 命令。

## 8. Check 与 Apply 的差异

| 行为 | `check` | `apply` |
|---|---:|---:|
| 调用维护模型 | 是 | 是 |
| 生成 Facts / Events 计划 | 是 | 是 |
| 修改源记忆 | 否 | 是 |
| 执行确定性去重和消息归档 | 否 | 是 |
| 写入 mutation 审计 | 否 | 是 |
| 检查语义索引一致性 | 是 | 是 |
| 修复语义索引 | 否 | 是 |

`check` 是预演，不是离线静态检查；它仍会产生模型 API 调用和相应成本。

## 9. 关键实现位置

| 文件 | 职责 |
|---|---|
| `pupu/maintenance/__init__.py` | 每日触发门禁和公开 API |
| `pupu/maintenance/runner.py` | 顶层编排、模式分支、报告和运行记录 |
| `pupu/maintenance/snapshot.py` | 会话快照与 evidence 原文回查 |
| `pupu/maintenance/model_compaction.py` | Summary、Event、Fact 阶段编排 |
| `pupu/maintenance/model_call.py` | Maintenance JSON 调用、解析与错误分流 |
| `pupu/maintenance/error_log.py` | Raw response 诊断日志、权限、截断和保留策略 |
| `pupu/maintenance/fact_compaction.py` | Fact 候选、动作校验和应用 |
| `pupu/maintenance/event_compaction.py` | Event 改写、merge 和 rollup |
| `pupu/maintenance/audit.py` | mutation fingerprint 与 before/after 审计 |
| `pupu/maintenance/prune.py` | 热消息归档与旧任务清理 |
| `pupu/maintenance/metrics.py` | 信息密度指标 |
| `pupu/storage/event_threads.py` | Event merge、alias 和 rollup 持久化 |
| `pupu/semantic_index/projection.py` | 源记忆到 semantic card 的投影 |

## 10. 运维判断标准

每日运行应重点观察：

- `status='success'` 的 `maintenance_runs` 是否每天出现一次。
- 自动 merge/archive 的 `memory_mutations` 是否都有完整 before/after 和 evidence。
- `fact_evidence_coverage` 是否接近 100%。
- `projection_chars` 是否在记忆增长时保持可控。
- `compression_ratio` 是否随 rollup 增长而提高。
- merged event threads 是否仍能通过旧 key 查询。
- semantic tidy 是否持续返回一致状态。

`raw_evidence_chars` 当前只统计 `event_steps` 的 summary、cause 和 reflection，不包含原始消息全文。因此 `compression_ratio` 是事件证据相对当前投影的工程指标，不是全库无损压缩率。
