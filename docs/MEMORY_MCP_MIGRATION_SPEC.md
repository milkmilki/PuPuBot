# PuPu 记忆 MCP 与 DeepSeek Harness 迁移规格

状态：提案，等待范围确认；尚未实现服务、安装包或记忆导出。

2026-10-19 产品方向补充：用户提出改为符合 DSH 插件规范、带原生 UI 的角色与记忆插件。面向 DSH 的主要交付改为 [PuPu 原生插件规格](DSH_PUPU_PLUGIN_SPEC.md)；本文继续定义记忆核心、MCP 通用入口与数据包要求。下文将 DSH 适配层称为“可选”的描述，仅适用于独立 MCP 发行包；完整 DSH 插件必须包含会话适配与 UI。

日期：2026-10-19。目标为 DeepSeek 官方仓库 `deepseek-ai/deepseek-harness`。

## 1. 目标与边界

把 PuPu 的长期记忆做成可以单独安装的本地 MCP 服务，并将具体记忆独立导出为可导入的数据包。另一台机器安装服务、导入同一个包后，应拥有相同的初始人物事实、关系和事件历史，并能继续积累新记忆。

初始假设：

- 保留当前 PuPu 的运行方式；先在独立数据目录验证迁移。
- 第一版面向单机、单用户，使用 stdio MCP 和 SQLite，不引入云服务。
- 同一记忆库跨 DSH 会话共享；人物身份与 DSH 会话 ID 分开管理。
- 同一角色可跨会话共享记忆，不同角色分别隔离。每个 DSH 会话显式绑定普通助手或角色扮演模式；导入、检索角色资料本身不启用角色扮演。
- 一个原 PuPu 实例对应一个记忆库，不自动混合多个实例的 `owner` 和 `instance`。
- “相同记忆”指源数据及其归属一致，不保证更换模型后回复逐字一致。
- MCP 承载通用记忆接口；DSH 适配层负责自动记录对话、召回和触发整理。
- 定时任务可作为历史数据保留；导入不自动恢复执行。QQ 收发、桌宠、主动发消息和语音不属于本次迁移。
- 人设可作为独立可选附件导出，但导入记忆不会自动改写宿主的系统指令。

## 2. 已核实的实现基础

本地代码：

| 位置 | 迁移意义 |
| --- | --- |
| `pupu/memory.py` | 兼容导出入口，实际存储实现在 `pupu/storage/` |
| `pupu/storage/db.py` | SQLite 表结构、迁移和初始化；当前依赖 `InstanceContext`，初始化还会读取角色种子事实 |
| `pupu/storage/people.py` | 人物、`owner` / `instance` / QQ 身份和事件人物关联 |
| `pupu/storage/facts.py`、`event_threads.py` | 人物事实与持续事件链的读写 |
| `pupu/agent.py` | 消息保存、回复前召回、批次 review 编排；当前默认 review 阈值为 30 轮 |
| `pupu/persona/review_prompt.py`、`review_parser.py`、`review_followups.py` | 记忆抽取约束、结果解析、事件和任务更新 |
| `pupu/semantic_index/` | SQLite 源数据投影、embedding 和召回缓存；部分日志当前写 stdout，移植时须改为 stderr |
| `pupu/maintenance/` | 事实与事件整理、证据、并发校验和审计 |
| `docs/MEMORY_MAINTENANCE.md` | 主库、证据、整理审计、可重建索引四层设计 |

只读统计的三个现存实例：

| 实例 | 人物事实 | 事件线 | 事件节点 | 摘要 | 热消息 + 归档消息 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `8623da76` | 10 | 0 | 0 | 0 | 2 + 0 |
| `bd7dae8d` | 110 | 33 | 175 | 279 | 442 + 2904 |
| `cc3120f8` | 194 | 31 | 199 | 253 | 510 + 2710 |

以上为检查时的快照，运行中的实例可能继续变化。第一个实例缺少部分新表，导出器必须识别旧结构，不能对源库直接调用 `init_db()` 升级。已检查的事件节点、事件人物关联、fact evidence 均未发现孤立父引用；这不等于完成了全部数据完整性验证。

DSH 官方依据：

- [MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/mcp/mcp-client/README.md)：每个服务一条插件配置，支持 stdio 与 Streamable HTTP；工具名称为 `mcp__<serverName>__<tool>`。MCP prompt templates 不受支持，因此记忆使用规则不能只放在 prompt template 中。
- [Agent lifecycle](https://deepseek-harness.github.io/deepseek-harness/en/reference/agent-lifecycle)：持久会话数据使用 `session/event`，运行时协调使用 `agent/*`。
- [System prompt](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/system-prompt/README.md)：支持按作用域注册上下文贡献；记忆正文应按数据注入，并关闭模板插值。
- [官方 README](https://github.com/deepseek-ai/deepseek-harness)：目前处于开发预览，兼容性可能变化。适配层需记录实际验证的版本或 commit，不能仅写“兼容最新版”。

源码核查基线为 `687f282e8625c6b8d11ff58211a90524be2e4317`。尚未运行 DSH 集成验证。

## 3. 组件与依赖

```text
DSH 会话
  -> PuPu DSH 适配层：记录消息 / 回复前召回 / 触发 review
  -> PuPu Memory MCP：输入校验 / 记忆服务
  -> SQLite：人物 / 事实 / 事件 / 摘要 / 证据 / 审计
  -> 可选 embedding：可重建 semantic_cards

原 PuPu 实例 --只读快照导出--> .pupumem 数据包
其他安装 --校验并导入--> 各自独立的记忆库
```

Python 核心与 MCP 使用同一发行包，暂定名称 `pupu-memory-mcp`，模块名 `pupu_memory`。使用项目现有的官方 Python MCP SDK；本机 bundled 环境实际安装版本为 `mcp 2.0.0`，发布约束以协议测试为准。embedding 网络客户端作为可选依赖。

记忆核心不依赖 QQ transport、Console、桌宠或原聊天主循环。把数据库路径、角色显示信息、时间来源和模型调用改为显式依赖；不再要求安装者创建完整 PuPu 实例。通用核心不内置某个用户的记忆或默认角色自述。

按模块逐步抽出核心，原 PuPu 通过兼容入口使用同一实现，避免长期维护两套存储逻辑。每个抽取阶段先通过回归验证，再迁移下一组模块。

DSH 适配层作为单独的可选插件，复用 DSH 已配置的模型能力执行受约束的 review。记忆 MCP 本身不要求 DSH 将 API 密钥传给服务。其他 MCP 宿主可自行调用 prepare/apply 接口；只安装通用 MCP，不承诺宿主自动保存每次对话。

## 4. MCP 接口草案

服务启动时绑定一个数据目录和记忆库；普通工具参数不能跨库选择任意文件。

| 工具 | 输入 | 返回与规则 |
| --- | --- | --- |
| `memory_status` | 无 | 版本、库标识、各类计数、索引状态和待整理进度 |
| `memory_recall` | `query: str`，`person_keys: list[str] = []`，`limit: int = 5` | 有界召回结果；每条包含类型、稳定引用、正文、原人物归属、时间、置信度、来源和命中依据；无 embedding 时使用本地检索 |
| `memory_get` | `reference: str`，`limit: int = 20`，`cursor: str | None = None` | 单条事实或事件详情及分页证据；游标绑定当前库和查询 |
| `memory_ingest` | `source: str`，`session_id: str`，`messages: list[MessageInput]` | `MessageInput` 必须包含稳定 `external_id`、角色、正文、说话人和时间；以来源 + 会话 + 消息 ID 去重 |
| `memory_prepare_review` | `session_id: str`，`max_turns: int = 30` | 有界待整理消息、候选事实和事件、结构化输出 schema、`review_id`；服务保存候选 ID 与版本 |
| `memory_apply_review` | `review_id: str`，`result: ReviewResult` | 校验后的 summary、fact updates、event updates；只接受该 review 的候选引用，返回变更计数、跳过原因和审计引用 |

约束：

- 工具 schema 显式声明文本长度、列表大小、枚举、范围与必填字段。
- 成功统一返回 `{"ok": true, "data": ...}`；失败返回结构化 `error.code` / `message`，并使用 MCP 的错误标志，不能把异常伪装为成功。
- 错误码至少覆盖 `VALIDATION_ERROR`、`NOT_FOUND`、`SCOPE_MISMATCH`、`CONFLICT`、`BUSY`、`UNSUPPORTED_VERSION`。
- 不暴露任意 SQL、文件读写或直接修改全库的工具。导入、导出、备份和索引重建由本地 CLI 提供。
- review 提交检查候选版本；网络推理期间不持有数据库写锁。失败不推进 review 游标，相同提交重试不重复写入。
- SQLite 是唯一事实源。索引失败不回滚已经成功的源记忆写入，而是明确报告待修复状态。

代码风格延续 Python 类型标注、snake_case、小函数和显式依赖，例如：

```python
def recall_memories(
    query: str,
    *,
    library: MemoryLibrary,
    person_keys: tuple[str, ...] = (),
    limit: int = 5,
) -> RecallResult:
    ...
```

最终输入输出类型与 JSON Schema 随包发布；本表为实现前契约草案。

## 5. 可导入记忆数据包

`.pupumem` 为 ZIP 容器，包含版本化 `manifest.json`、结构化 JSON/JSONL 和可选附件。manifest 记录格式版本、稳定来源库 ID、来源实例、导出时间、导出模式、schema 指纹、表计数、文件校验和及功能声明。校验和用于发现损坏，不代表发布者身份已获认证。

两种导出模式：

| 模式 | 内容 | 用途 |
| --- | --- | --- |
| `full` | 人物、事实、事件及全部节点、摘要、消息及归档、fact evidence、rollup、alias、整理审计；好感度及任务历史按声明保留 | 自己迁移并保留可追溯证据 |
| `seed` | 选择后的人物、事实、事件、摘要、必要证据文本；支持一致的身份替换；默认不附整库原始聊天 | 分发共同的初始记忆 |

API 密钥、原运行配置、QQ token、日志、定时任务执行器、MCP 配置都不进入数据包。`semantic_cards` 与 FTS 属于缓存，目标环境自行重建；不要求安装者拥有原 embedding 模型。

### 身份与引用

- `owner`、`instance`、QQ 人物、会话和事件均保留来源身份，不因为安装者不同就把原经历当成新安装者的经历。
- 支持显式身份映射；替换人物标识时必须连同 facts、关系、event_people、消息和证据中的引用一致替换。
- 稳定来源库 ID + 实体类型 + 来源记录 ID 用于追踪来源，不能只用本地自增 ID 去重。
- 新库还原可以保留来源 ID。合并到已有库时，必须构建完整 ID 映射，修复 thread/current step/task/superseded/rollup/evidence 等引用；不能直接 `INSERT OR REPLACE`。
- 消息范围证据不能只替换范围两个端点：合并时必须按来源消息集合核对，防止非连续 ID 错指其他对话。seed 未携带原消息时，明确标记来源摘录和缺失原文，不能制造有效外键的假象。

### 导入行为

1. `inspect` 显示来源、模式、人物、计数和待映射身份。
2. `import --dry-run` 校验版本、大小、文件白名单、校验和、完整引用关系和冲突，不写目标库。
3. 正式导入先保存目标备份，使用事务应用源数据和来源映射。
4. 默认冲突策略为报告并保持本地内容；非冲突记录可合并。不同实例默认导入为不同库。
5. 同包重复导入无重复记录；更新后的包不能静默覆盖目标端的新记忆。
6. 源数据提交后再重建索引；索引失败应保留可用的本地检索并返回报告。

full 用 SQLite backup API 取得一致快照，转换和旧 schema 兼容处理仅发生在临时副本，不升级或写入现有实例库。

## 6. DSH 自动记忆适配

- 监听持久会话事件，只收集用户与助手的可见消息；不把思维链、系统指令或工具执行流当聊天长期写入。
- 使用稳定事件 ID 幂等 ingestion；重启、重连、回放都不能重复记忆。
- 将 DSH `session_id` 映射到当前库中的会话，同时显式绑定当前人物；开新会话仍能召回同一个人的长期记忆。
- 在回复前召回并注入有长度预算、带来源的记忆上下文，按数据处理。记忆文本不作为新系统权限，也不参与模板插值。
- 达到批次阈值后，调用 prepare、使用 DSH 模型生成结构化 review，再调用 apply。review 失败保留待处理状态。
- 每日整理复用已有确定性校验、证据和审计逻辑；只在宿主或服务实际运行期间触发，不宣称关闭应用后仍会执行。
- stdio 启动仅启动服务，不自动导入、整理或调用外部模型。DSH 可能先启动探测子进程，初始化必须可重入且无破坏性副作用。

首版 DSH 接入配置的预期形状：

```yaml
- id: pupu-memory
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: pupu_memory
    transport: stdio
    command: 'C:/path/to/memory-env/Scripts/pupu-memory-mcp.exe'
    args: ['serve', '--data-dir', 'C:/path/to/memories', '--library', 'pupu']
```

此配置依赖后续实现的入口，当前不能运行；自动记忆还需要上述适配插件，不能以“工具已出现在列表”替代自动流程验收。

### 6.1 多角色隔离与会话模式

补充于 2026-10-19：用户进一步明确，需支持同一 MCP 接入多个人物的记忆，并区分当前正在扮演哪个人物或没有在扮演。

需要区分两个不同维度：

- `character_id`：这是谁拥有的记忆、当前由谁说话。例如“仆仆记得用户喜欢咖啡”。
- `subject_person_key` / `object_person_key`：这条事实在描述谁。例如上述事实描述的是用户，不代表所有角色都共享这条经历。

现有每实例一个数据库可以作为角色记忆库隔离的起点；库内的 `people` 表是被记住的人物索引，不等于当前扮演角色的选择器。

DSH 适配层持久保存并在每轮使用会话绑定：

```text
conversation_id      # 宿主的稳定会话标识
mode                 # assistant | roleplay
character_id         # roleplay 时必须指定；assistant 时为空
user_id              # 当前实际交谈对象；独立于角色和会话
scope_version        # 切换后递增，拒绝旧作用域的错误应用
```

| 会话模式 | 自动召回 | 自动写入 |
| --- | --- | --- |
| 普通助手 | 默认不读取角色私人记忆；普通助手记忆如启用则单独保存 | 不写入角色库 |
| 扮演仆仆 | 仆仆被授权可见的记忆 | 仆仆库，保留用户及消息来源 |
| 扮演其他角色 | 该角色被授权可见的记忆 | 该角色库 |

约束：

1. 会话绑定由宿主适配层提供并由服务校验。标准 MCP 连接本身不会自动提供可信的 DSH 会话、真实用户或角色模式；不能根据一条工具参数就假定已获得跨角色访问权限。
2. 通用部署可以每个角色启动一个固定库的 MCP 子进程；共享一个多角色服务时，须使用宿主分配的作用域凭据或等效连接绑定。模型不能靠自由填写 `character_id` 绕过范围。
3. 服务不保存一个供所有聊天共享的“当前角色”。两个并行会话及后台 review 分别持有自己的作用域。
4. 角色设定与当前模式由适配层传给 DSH；检索返回的记忆正文仍作为带归属的数据处理。加载了某个角色的数据包不代表模型已在扮演该角色。
5. 明确的角色选择、切换命令或退出指令改变绑定；只在聊天中提到角色名字、引用对白或查询其资料不改变绑定。自然语言解析有歧义时保留当前绑定并澄清。
6. 消息写入时固定其模式和角色归属；review 固定来源批次的作用域。切换后，旧任务只能按原范围完成或被取消，不能把旧批次写到新角色。
7. 默认采用一条会话绑定一个角色的上下文。普通助手与各角色使用分别隔离的历史；需要在同一界面切换时，适配层也必须切换底层上下文。
8. 仅删除旧角色提示或停止旧库检索，无法让模型忘记同一长会话中已经读过的内容。数据库隔离只保证记忆服务边界内的访问范围；严格角色隔离还要求历史、摘要、召回缓存和后台任务一并隔离。
9. 用户明确要求普通助手查阅角色资料时，可走独立的、授权的只读资料查询路径，标明来源；不因此启用角色扮演或把查询过程写成角色经历。

新增验收：同时运行普通助手、角色 A、角色 B 会话；逐一验证读写范围、跨会话身份稳定性、重启恢复、切换期间的延迟 review 和缓存作用域。A 的私人记忆不能经 B 的 recall/get/证据接口或缓存返回；普通聊天与引用角色名字不能误触发角色切换。

## 7. 结构与预期命令

以下目录和命令是交付目标，目前尚未创建对应实现：

```text
packages/pupu-memory-mcp/
  pyproject.toml
  src/pupu_memory/       # 核心、MCP、CLI、数据包格式
  tests/                # 导入引用、幂等、协议与核心回归
integrations/dsh-pupu-memory/
  package.json
  src/                  # DSH 会话与上下文适配
  tests/
docs/MEMORY_MCP.md       # 安装、迁移、共享种子与恢复操作
```

```powershell
# 独立安装与测试（实现后）
python -m pip install -e ./packages/pupu-memory-mcp
python -m unittest discover -s ./packages/pupu-memory-mcp/tests
python -m pip wheel --no-deps ./packages/pupu-memory-mcp --wheel-dir ./dist

# 从指定实例导出；来源库 ID 必须稳定，不能随每次导出变化
pupu-memory-mcp export-pupu --instance-dir ./instances/cc3120f8 --library-id pupu-cc3120f8 --mode full --output ./exports/pupu.pupumem
pupu-memory-mcp inspect ./exports/pupu.pupumem
pupu-memory-mcp import ./exports/pupu.pupumem --data-dir ./local/memory-mcp --library pupu --dry-run
pupu-memory-mcp import ./exports/pupu.pupumem --data-dir ./local/memory-mcp --library pupu
pupu-memory-mcp serve --data-dir ./local/memory-mcp --library pupu

# 现有 PuPu 回归
.\ForFun\Scripts\python.exe -m unittest discover tests
.\ForFun\Scripts\python.exe -m compileall -q pupu pupu_console tests
git diff --check
```

示例实例 ID 仅演示命令格式，不代表已经选定、导出或分发该实例。

## 8. 实施顺序与验收

| 顺序 | 交付 | 必须通过的验证 |
| --- | --- | --- |
| 1 | 独立核心与显式配置；逐组迁移存储模块 | 临时数据库测试与现有相关测试通过；新核心不导入 transport、Console 或聊天循环 |
| 2 | full/seed 导出、inspect、dry-run、新库导入 | 源库只读；新旧 schema 均支持；主数据计数、人物、事件状态与证据引用一致 |
| 3 | 幂等和非冲突合并 | 本地 ID 冲突、非连续消息范围、人物重映射、重复导入、包升级冲突与事务失败均有回归用例 |
| 4 | stdio MCP | 用官方客户端完成 initialize/list/call；全程 stdout 仅有协议；无 embedding 仍可召回 |
| 5 | DSH 适配 | 在记录版本的官方 DSH 中实测跨会话召回、新消息自动入库、review 更新、重启不重复写入 |
| 6 | wheel、安装示例和本地迁移包 | 干净虚拟环境仅安装 wheel，即可导入和启动；原 PuPu 回归通过；提供可复现验证报告 |

实现以小步交付，每步先写能揭示真实失败的测试，再实现并回归。测试数据采用合成夹具；真实数据库只在本地只读快照上验证，不加入仓库或安装包。

最终成功标准：新安装可导入同一数据包并查到同样的关键人物事实和完整事件链；在 DSH 中继续对话后，能更新已有事件并形成新记忆；重复导入、重连、重启和模型失败都不会破坏已有记忆。

## 9. 操作边界与待确认项

- 始终：先校验再写入、保留来源归属、备份目标、保留证据审计、运行相关测试。
- 范围确认后可执行：增加独立发行包、新目标库 schema、原 PuPu 兼容入口和 DSH 适配。上述属于本规格的实现范围。
- 另行确认：覆盖现有实例原库、主动合并不同实例身份、将真实记忆公开分发、公开发布软件包，以及恢复原定时任务执行。
- 禁止：提交密钥和真实记忆；未经说明把历史人物经历归给新用户；将记忆文本视为可执行代码或宿主权限。

仍需确认的产品选择：本提案将“通用 MCP + 可导入数据包 + DSH 自动记忆适配”作为完整交付范围。首次真实迁移时再选择具体实例，以及 full 自用迁移还是 seed 分发；不影响先实现通用组件。
