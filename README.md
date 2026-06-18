# PuPuBot

PuPuBot 是一个面向长期陪伴聊天的 AI bot 实验项目。它可以运行在命令行、QQ 私聊/群聊和本地 Web 控制台里，核心目标不是只回答当下这一句话，而是维护一段持续关系里的记忆、事件、约定和后续进展。

![PuPuBot 记忆系统设计图](docs/assets/event-chain-design.svg)

## 项目亮点

- **长期角色陪伴**：支持角色人设、好感度、按人物归属的长期事实、对话摘要、主动消息和定时跟进。
- **事件链记忆**：用 `event_threads` + `event_steps` 记录持续事件的状态演进。
- **增强召回归并**：用 SQLite FTS5 倒排召回 + 关键词 overlap + 近期/状态/置信度重排，减少重复事件线。
- **人物索引**：用 `people` + `event_people` 把事件线和参与人物绑定，私聊、群聊和多实例场景能更稳地区分“谁做了什么”。
- **实例化运行**：每个 bot 都运行在 `instances/<id>` 下，CLI、NapCat、QQ 官方和 Console 共用同一套实例模型。
- **可视化记忆图谱**：`/events url` 可导出自包含 HTML，内置 `events` / `facts` 两种视图，分别查看事件链进展和人物事实关系。
- **可选 memU 长期记忆缓存**：SQLite 是主记忆库；memU 只作为可删除、可重建的 RAG 缓存，用来按需召回相关摘要、事实和事件线快照。
- **任务与提醒**：支持 scheduled tasks，并把任务取消、错过、重排等变化写回事件线。

## 记忆系统设计

PuPuBot 的记忆系统分成两层：本地 SQLite 主记忆库，以及可选的 memU 召回缓存。SQLite 始终是唯一事实源；memU 不负责决定事件或 facts 是否存在，只保存可检索、可删除、可重建的长期记忆副本。你可以把它理解成“主库 + 召回缓存”的组合：主库负责真实，缓存负责更容易想起来。

### 主记忆库

很多长期陪伴对话里的事情不会停在单次记录上：一个约定会被推迟，一个计划会完成，一次互动会变成后续话题。如果每次都创建新的扁平事件，记忆很快会变得重复、碎片化，也更难被模型稳定召回。

核心表大致分为几类：

- `messages`：原始聊天记录，带 `source`、说话人 key、昵称和 QQ 号等上下文。
- `summaries`：batch review 后写入的批次摘要。
- `person_facts`：稳定长期事实的主表，按 `subject_person_key` / `object_person_key` 区分“某个人的事实”和“两个人之间的关系事实”。
- `event_threads`：持续事件线，保存标题、当前状态、生命周期状态、置信度、跟进提示、合并提示、关联任务等。
- `event_steps`：事件线里的进展节点，保存状态变化摘要、触发原因、发生时间和可选反思。
- `people` / `event_people`：人物索引，把事件线、事件节点和固定人物身份关联起来。
- `scheduled_tasks`：提醒和定时任务；任务完成、取消、错过、重排会追加 `system` 类型事件节点。

step 类型有四种：

| step_type | 含义 |
| --- | --- |
| `user` | 用户的话或行为推动了事件变化 |
| `instance` | bot 的话或行为推动了事件变化 |
| `time` | 时间自然流逝带来的推测状态，必须保留“可能/推测”语气 |
| `system` | 系统维护、任务取消/错过/重排等导致的状态变化 |

### 写入流程

正常聊天不会每句话都立刻写长期记忆，而是由 batch review 在累计到一定数量的 `chat` 消息后统一整理：

1. 程序把消息预处理成“人物名：发言 `<end>`”格式，避免模型把 QQ 号、昵称和实例名混在一起。
2. 先用 `find_related_event_threads()` 检索候选事件线，并把候选标题、当前状态和最近节点放进 review prompt。
3. review 模型输出 `summary`、`person_facts`、`event_updates` 和 `task_updates`；长期 facts 只写入 `person_facts`。
4. `event_updates` 优先 `append_step` 到已有事件线；只有明显无关才 `create_thread`。
5. 写入本地 SQLite 后，如果启用了 memU，再把本轮 `summary` / `person_fact` / `event_thread` 快照写成可召回缓存。memU 写入失败只影响召回，不会阻断主库写入。

### 召回流程

聊天时有两种召回路径：

- 未启用 memU：直接从 SQLite 读取近期 summaries、当前对话人物相关的 `person_facts` 和当前事件线，注入 system prompt。个人事实按当前用户/实例读取；关系事实只在参与双方都匹配时注入，避免把 A 和实例的关系带给 B。
- 启用 memU：用当前消息和最近聊天作为 query，从 memU 召回相关 `summary` / `person_fact` / `event_thread` 缓存条目，再注入“本轮自然想起的记忆”。

事件线自己的归并检索不依赖 memU。`find_related_event_threads()` 先用 SQLite FTS5 召回候选，再按关键词 overlap、事件状态、近期活跃度、置信度和人物匹配进行重排。`/events search --debug <query>` 可以看到每条候选的分数组成。

### 整理与同步

`/tidy` 和每日维护走两条不同职责：

- 本地维护会合并摘要、轻量更新事件线标题/当前状态/置信度、整理 facts；它不会删除或 drop 本地事件线。
- memU tidy 只整理 memU 缓存副本，删除明显重复或低价值的 memU 条目，不删除本地 facts、事件线或聊天记录；整理后会检查 active/scheduled 等事件线缓存是否缺失，缺了就从 SQLite 主库补齐。

这样做的原则是：本地库负责“真实记忆”，memU 负责“更容易想起来”。

### 可视化

记忆图谱通过 `/events url` 查看：

- `events` 视图：展示人物、事件线和进展节点，适合检查事件链是否正确归并。
- `facts` 视图：展示人物 facts 和关系 facts，适合检查人物画像与人物关系。

## 快速开始

### 1. 安装依赖

```powershell
python -m venv ForFun
.\ForFun\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 配置

第一次启动时，如果根目录没有 `pupu.yaml`，PuPuBot 会自动从 `pupu.yaml.example` 生成一份。随后编辑 `pupu.yaml` 即可。

最小可用配置通常只需要填写：

- `llm.provider` 以及对应 provider 的 API key，例如 `llm.deepseek.api_key`。
- `user.owner_ids`：如果要使用 QQ owner-only 命令，填你的 QQ 号。
- `instance.qq_mode`：可选 `cli`、`napcat` 或 `official`。
- `napcat.port`：使用 NapCat 时填写反向 WebSocket 端口。

API key 不会放进仓库追踪文件。实例相关文件由启动器或 Console 创建在 `instances/<id>/` 下。`pupu.yaml`、`data/`、`instances/`、日志和 SQLite 数据库都已加入 `.gitignore`。

### 3. 创建或选择实例

```powershell
python start.py
```

`start.py` 每次都会要求选择已有实例或创建新实例。项目不再保留根目录默认 bot；每次运行都绑定到一个实例目录，实例拥有自己的 `instance.json`、`persona.json`、生成的 `.env.qq`、`data/pupu.db` 和 `data/memu.db`。

Windows 下可以双击 `启动仆仆.bat`，它只是 `ForFun\Scripts\python.exe start.py` 的启动包装。

如果只想进入 CLI，也可以直接运行：

```powershell
python -m pupu.cli
```

### 4. CLI 命令

常用命令：

| Command | Description |
| --- | --- |
| `/events` | 查看当前事件线 |
| `/events detail <key>` | 查看某条事件线的完整进展 |
| `/events search <query>` | 搜索相关事件线 |
| `/events search --debug <query>` | 查看召回评分细节 |
| `/events url` | 导出独立记忆图谱 HTML，包含 events / facts 两种视图 |
| `/tidy` | 整理长期记忆；启用 memU 时清理 memU 缓存并从主库补齐事件线缓存 |
| `/score` | 查看好感度 |
| `/history` | 查看最近聊天 |
| `/quit` | 退出 CLI |

### 5. 启动 PuPu Console

```powershell
python -m pupu_console
```

打开 [http://127.0.0.1:8770](http://127.0.0.1:8770)。Console 可以创建和管理实例、启动/停止 bot 子进程、导入 SQLite 记忆库，并编辑 soul/persona 预设。记忆图谱请在 CLI 或 QQ 中使用 `/events url` 导出查看。

Windows 下也可以双击 `启动仆仆控制台.bat`。

### 6. 启动 QQ Bot

创建或编辑一个 `qq_mode=napcat` 或 `qq_mode=official` 的实例，然后运行 `python start.py` 并选择它。多实例运行推荐使用 PuPu Console 启停。

NapCat 实例会根据 `pupu.yaml` 生成实例自己的 `.env.qq`。NapCat 反向 WebSocket 地址配置为 `ws://127.0.0.1:<port>/onebot/v11/ws`。

## 项目结构

```text
pupu/                       Core memory, agent, LLM, tools, tasks, event graph
pupu/storage/               SQLite schema and storage adapters
pupu/memory_index/          Optional memU adapter and tidy sync
pupu_console/               Local web console and multi-instance process manager
plugins/pupu_support/       NoneBot command and message handlers
docs/                       Extra guides and architecture notes
tests/                      Unit tests
```

## 测试

在仓库根目录运行：

```powershell
python -m unittest discover tests
```

不要直接使用裸的 `unittest discover`，某些工作区里历史临时文件可能会被误收集。

## 安全说明

这个仓库只适合发布源码。不要提交：

- `pupu.yaml`, instance `.env.qq`, instance `instance.json` if it contains private account data
- SQLite memory databases
- logs, backups, exported event graph pages
- instance folders and soul presets containing private persona or account data

如果你曾经把 API key 或聊天数据库提交到任何远程，请在公开仓库前轮换相关 key。
