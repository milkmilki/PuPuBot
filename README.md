# PuPuBot

PuPuBot 是一个面向长期陪伴聊天的 AI bot 实验项目。它可以运行在命令行、QQ 私聊/群聊和本地 Web 控制台里，核心目标不是只回答当下这一句话，而是维护一段持续关系里的记忆、事件、约定和后续进展。

![PuPuBot 事件链记忆设计图](docs/assets/event-chain-design.svg)

## 项目亮点

- **长期角色陪伴**：支持角色人设、好感度、用户事实、自我事实、对话摘要和主动消息。
- **事件链记忆**：把传统的 `important_events` 升级为 `event_threads` + `event_steps`，用事件线记录持续事件的状态演进。
- **增强召回归并**：用 SQLite FTS5 倒排召回 + 关键词 overlap + 近期/状态/置信度重排，减少重复事件线。
- **实例化运行**：每个 bot 都运行在 `instances/<id>` 下，CLI、NapCat、QQ 官方和 Console 共用同一套实例模型。
- **可视化事件图谱**：`/events url` 可导出自包含 HTML，Console 也内置事件图谱页，支持横向事件链和中心发散布局。
- **可选 memU 长期记忆索引**：可以把长期记忆同步到 memU，用 RAG 方式按需召回。
- **任务与提醒**：支持 scheduled tasks，并把任务取消、错过、重排等变化写回事件线。

## 事件链记忆设计

事件链是这个项目最核心的记忆设计。

传统做法会把“重要事件”保存成一条条扁平记录。问题是，长期陪伴对话里的事情往往会继续发展：一个约定会被推迟，一个计划会完成，一次互动会变成后续话题。如果每次都创建新的 important event，记忆很快会变得重复、碎片化，也更难被模型稳定召回。

PuPuBot 的事件链把记忆拆成两层：

- `event_threads`：一条持续事件线，保存标题、当前状态、状态、置信度、跟进提示、合并提示、关联任务等。
- `event_steps`：事件线里的进展节点，保存状态变化摘要、触发原因、发生时间和可选反思。

step 类型有四种：

| step_type | 含义 |
| --- | --- |
| `user` | 用户的话或行为推动了事件变化 |
| `instance` | bot 的话或行为推动了事件变化 |
| `time` | 时间自然流逝带来的推测状态，必须保留“可能/推测”语气 |
| `system` | 系统维护、任务取消/错过/重排等导致的状态变化 |

Batch review 会先根据最近对话检索候选事件线。如果候选相关，优先追加 `append_step`；只有明显无关时才创建新 `create_thread`。这样同一件事会沿着一条链自然生长，而不是散落成很多相似事件。

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
| `/events url` | 导出独立事件图谱 HTML |
| `/events migrate` | 让模型把旧 important events 迁移成事件链 |
| `/events migrate simple` | 机械地一条旧事件迁移成一条事件线 |
| `/tidy` | 整理长期记忆，启用 memU 时会同步本地库和 memU |
| `/score` | 查看好感度 |
| `/history` | 查看最近聊天 |
| `/quit` | 退出 CLI |

### 5. 启动 PuPu Console

```powershell
python -m pupu_console
```

打开 [http://127.0.0.1:8770](http://127.0.0.1:8770)。Console 可以创建和管理实例、启动/停止 bot 子进程、导入 SQLite 记忆库、编辑 soul/persona 预设，并查看事件图谱。

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
integrations/stardew_npc/   Experimental Stardew Valley NPC bridge
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
