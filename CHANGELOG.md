# 更新日志

## 2026-06-26

### 开放群仲裁稳定性

- 修复 actor 开放群订阅启动时保留旧 decision cursor 的问题，避免 silence 切换或重启后误消费上一轮仲裁结论。
- 仲裁判定 `speaker=none` 或选中其它 bot 时，会清理当前轮本地群聊 buffer，防止下一轮把旧测试消息和新消息一起送进聊天模型。
- 清理群聊 buffer 前会校验 decision 的 `since_message_id`，避免旧 decision 误删已经进入 buffer 的更新消息。
- 仲裁 long-poll 不再返回已过期的 `group_decisions`。
- 补充开放群仲裁回归测试，覆盖 cursor 推进、未选中清 buffer、旧 decision 不清新 buffer、过期 decision 过滤。
- 修复编码审计测试自身包含 mojibake marker 导致全量回归自命中的问题。

### 语义索引内化

- 用 PuPu 内置 SQLite 语义索引作为长期记忆召回层：新增 `semantic_cards` 和 `semantic_sync_log`，SQLite 继续作为唯一事实源，语义索引只保存可重建 card 与 embedding。
- `/recall`、`/tidy`、batch review sync、facts 写入前候选召回和自动维护都使用内置 semantic index。
- `pupu.yaml.example` 使用 `semantic_index:` 配置，视觉工具复用 `semantic_index.embed_api_key` / `semantic_index.embed_base_url`。
- 删除外部记忆 SDK runtime 和相关依赖文件，项目只使用 PuPu 自带的 semantic index。
- 新增 `scripts/rebuild_semantic_index.py`，可从各实例 SQLite 事实源全量重建 `semantic_cards`。
- README 更新为纯 `semantic_index:` 配置与重建说明。

### 维护性收敛

- 测试启动时统一把 Python 临时目录指向 `tests/_tmp/runtime`，降低 Windows 用户目录、权限和 sandbox 差异导致的测试不稳定。
- `tests.helpers` 新增统一测试临时目录 helper，后续测试可逐步迁移到同一套 scratch 目录。
- 新增 tracked 文本文件 UTF-8 / 常见 mojibake 审计测试，防止中文 prompt、文档和控制台文案被错误编码提交。
- 新增真实群聊日志回放测试，用同一段“小夫 / 仆仆 / 璐璐”群聊样本同时校验聊天 prompt、batch review 输入和仲裁上下文，防止 raw QQ 昵称污染、关系前缀回流和“双璐璐”人物合并回归。
- `deploy.bat` 不再强制 Python 3.14，改为优先 Python 3.13/3.12；语义索引已经内置，不再有额外记忆库安装步骤。
- 新增 `requirements.lock.txt` 记录当前验证环境的依赖快照。
- 从 `agent.py` 拆出 `pupu.review_parser`，集中处理 batch review JSON 清洗、修复和 normalize，并补充独立 parser 单元测试。
- 修复聊天 prompt 中 `speaker_qq` 单说话人消息没有优先使用固定人物名的问题，避免 raw QQ 昵称污染最近上下文。
- arbiter runtime 测试在清理统一临时目录前显式关闭 PuPu 日志 sink，避免 Windows 下日志文件句柄未释放导致目录删除失败。

## 2026-06-24

### 文档

- README 新增 MCP 工具调用流程图，说明外部 MCP server 如何通过 PuPu `ToolRegistry` 代理成普通 tool schema，以及工具循环中 `tool_use` / `tool_result` 如何逐轮追加到模型上下文。

### 工具

- 删除重复的 `mcp__media__look_at_image` 视觉工具，只保留 `mcp__media__describe_image` 作为唯一看图工具，减少模型工具选择噪声。

### LLM API provider 收口

- 移除 `codex_cli` provider、Codex CLI 子进程调用和 PuPu-for-Codex MCP server；后续模型接入只走 LLM API provider。
- `pupu.yaml.example`、桌宠 API key 设置和 provider 校验不再展示或接受 `codex_cli`。
- 外部 MCP server 保留为 PuPu 内置 stdio MCP client 管理，通过普通 tool schema 提供给 DeepSeek/Anthropic 等 API provider。

## 2026-06-21

### 关系阶段 Prompt 通用化

- 重写 `FAMILIARITY_PROMPTS`，从具体口癖和实例偏好改为更通用的关系状态描述，让不同实例都能复用同一套好感度关系提示。
- 进一步精简关系阶段提示，只保留每个阶段的核心关系质感和少量互动倾向，减少规则感。

### OneBot 命令发送修复

- 修复 NapCat actor 在处理 `/proactive status`、`/proactive force` 这类命令时等待发送 echo 导致 WebSocket 读循环自锁的问题；OneBot 事件现在后台派发，socket 会继续接收 NapCat 的 action 响应。

### Proactive 命令修复

- QQ/CLI 命令服务现在兼容裸 `proactive status`、`proactive force`，不会把这类文本送进聊天模型导致等待普通回复。
- 补齐 `proactive force` 执行路径，会立即生成一条主动消息并通过当前实例 sender 发送。

### 视觉工具

- 新增 `mcp__media__describe_image`，通过百炼/DashScope OpenAI-compatible 接口调用 `qwen3.6-flash` 识图，给 DeepSeek 等纯文本模型补充图片理解能力。
- `pupu.yaml.example` 新增 `vision` 配置块，支持 `model` 和 `timeout`；视觉工具直接复用语义索引的百炼配置，不再要求单独填写视觉 key。
- `describe_image` 支持 `query` / `question` / `prompt`，智能体可以带着“这是谁、图片在表达什么、画得怎么样”等具体问题看图，不再只能返回泛泛描述。
- 每个会话会短期缓存最近图片 URL（默认 30 分钟、最多 8 张），用户后续追问“刚才那张图”时，`describe_image` 可继续复用最近图片；若工具调用未显式传 `query`，会默认使用当前用户文本作为看图问题。
- 视觉工具会缓存已成功下载的图片 base64 内容，`look_at_image` 成功后 `describe_image` 可直接复用同一张图，避免 QQ/NapCat 临时图片 URL 二次下载失败；调用百炼/Qwen 视觉接口遇到 SSL EOF、连接中断、超时或 5xx/429 等瞬时错误时会自动重试。
- `describe_image` 默认优先把原始 http 图片 URL 交给百炼/Qwen，失败时再回退 base64，避免大图 base64 请求体导致 `RemoteProtocolError`、`WinError 10054` 或 `ReadTimeout`；同一会话中同一张图同一问题的识别文本会短期复用。
- 调用百炼/Qwen 视觉接口时会带上 `X-DashScope-OssResourceResolve: enable` 以支持 QQ/NapCat 临时图片 URL；400 错误会输出百炼响应体，方便区分 `InvalidURL`、格式不支持、图片过大等真实原因。
- `look_at_image` 聊天工具改为返回 Qwen 视觉文字描述，不再只返回 Anthropic 图片 content block，避免 DeepSeek/Codex 等纯文本链路误以为“看不到图”。

### 语义召回

- `recall_memories()` 对 `APITimeoutError`、连接失败、网络/限流等瞬时错误恢复 3 次重试日志，避免一次 recall 超时后直接放弃长期记忆检索。
- 语义召回检索 query 只使用当前轮用户内容，不再拼入 30 条近期上下文；近期上下文只用于最终对话模型生成回复，避免旧话题污染长期记忆召回。

### 默认记忆窗口

- 默认 batch review 触发批量从 10 条消息调整为 30 条消息，减少过短片段导致的碎片化总结。
- 默认聊天最近上下文窗口从 10 条消息调整为 30 条消息，让模型能看到更完整的短期对话。
- 默认语义召回 `retrieve_top_k` 从 6 调整为 5，事件线候选召回也同步从 6 调整为 5，降低 prompt 噪声。

### 分支协作

- 新增 `docs/branch-workflow.md`，明确 `main` 是后端事实来源、`siri` 是桌宠前端集成分支，后端改动应先落 `main` 再同步到 `siri`。
- README 增加分支协作文档入口，方便后续 agent 接手时先确认分支职责和同步方式。

### Proactive 与 CLI

- CLI 实例现在与 NapCat 实例一样会在 `proactive_enabled=true` 时启动 proactive loop，不再只在 QQ/NapCat 通道自动运行。
- CLI proactive 不再要求配置数字 owner QQ；主动消息、scheduled sender 和 wait-followup 追问会通过 CLI 输出回调打印到当前终端。
- `InstanceActor` 支持注入 `cli_send` 投递函数，CLI 入口用同一 actor runtime 接收普通回复、主动消息和延迟追问。
- 补充 actor 回归测试，覆盖 CLI proactive 自动启动、无 owner QQ 投递、主动消息投递和 proactive should-wait 追问投递。

### 钩子层

- 新增 `pupu.hooks` 进程内钩子层，支持注册同步或 async hook，并隔离 hook 异常，避免观察逻辑影响 bot 主流程。
- 新增 `instance.status` 状态钩子，覆盖 actor 的 `starting`、`running`、`stopping`、`stopped`、`failed` 生命周期事件。
- 新增聊天生命周期钩子：`chat.started`、`chat.reply_created`、`chat.error`，供桌宠 UI 显示 thinking/speaking/error 状态。
- 新增记忆整理钩子：`memory.review_started`、`memory.review_finished`，供 UI 展示 batch review 整理进度。
- `InstanceActor` 启停和启动失败路径会发出状态事件，并在失败路径清理 transport、后台任务和日志 sink。
- 补充 hook 生命周期测试的实例上下文与语义索引隔离，单独运行 `tests.test_hooks` 不再误触真实长记忆索引。

### 本地桌面客户端接入层

- 新增桌宠 API：`GET /api/desktop/status`、`POST /api/desktop/chat` 和 `WS /ws/desktop/events`，供本地桌宠读取实例状态、发送桌面聊天并订阅 hook 事件。
- `ProcessManager` 新增 `desktop_chat()`，在运行中的 actor 实例上下文内复用现有 `pupu.agent.chat()`，桌宠会话固定为 `desktop_owner`。
- hook 转发保持只读，注册在 Console lifespan 内，异常不会影响 agent 主流程。
- 增加桌宠 API 单测，覆盖 status、停止实例拒绝聊天、运行实例返回回复、WebSocket hook 转发和 actor 上下文复用。

### 开放群仲裁内嵌化

- 移除独立的 `pupu_console.arbiter_server` HTTP 服务，开放群仲裁现在内嵌在控制台 actor runtime 中运行。
- 新增 `pupu.arbiter_runtime`，负责进程内 observe、安静窗口 debounce、`run_judge` 调用和 decision 唤醒。
- `MessageBuffer` 不再访问 `127.0.0.1:18079`，也不再输出 arbiter 连接失败/低频探测日志。
- `/silence` 改为直接读写内嵌仲裁状态，静默状态持久化在 `instances/_shared/arbiter.db`，重启后继续生效。
- Console 顶部仲裁栏改为状态展示，删除外置仲裁服务的启动/停止/健康检查按钮。
- 新建实例和 `pupu.yaml.example` 不再生成 `arbiter_url`、`arbiter_base_url`、host/port/timeout 等外置服务配置。

### QQ 通道收口

- 移除 QQ 官方 Bot 适配器路径和旧 NoneBot 插件入口。
- 实例配置收口为 `cli` / `napcat` 两种 `qq_mode`，Console 表单和 `pupu.yaml.example` 不再展示 `official`、`qq_app_id`、`qq_app_secret`。
- 清理 `qqofficial:`、`c2c_`、`qqgroup_` 等官方通道身份分支；普通 QQ 号人物映射继续使用 NapCat 的 `qq:<号>`。
- 旧实例 JSON 中残留的官方配置字段会在读写实例配置时被剥离，避免继续污染新配置。
- 新实例不再生成 `.env.qq`；NapCat 端口以 `instance.json.port` 为准，host/token 使用 `pupu.yaml.napcat`。

### 单进程 Actor 运行时

- 删除旧的“一实例一 Python 子进程 + NoneBot 插件”运行时，Console 现在直接在同一 Python 进程内托管多个 `InstanceActor`。
- 新增 `pupu.actor` 运行时，包含 `InstanceContext`、消息 buffer、NapCat transport、scheduler、proactive 和 maintenance 任务管理。
- 新增轻量 OneBot v11 reverse WebSocket transport，每个实例继续监听 `ws://127.0.0.1:<port>/onebot/v11/ws`，不再依赖 NoneBot 多开。
- NapCat OneBot transport 启动时会先检查端口是否可绑定，并等待 uvicorn 确认绑定成功后才打印 listening；端口仍被占用时会同步失败并回滚实例启动状态，避免后台异步抛出 WinError 10048 后 Console 误判启动成功。
- NapCat 实例支持用数字 `bot_id` 约束允许连接的 QQ `self_id`；Console 实例设置页新增 Bot QQ/self_id 输入框，避免两个 NapCat 账号配错到同一端口后反复互踢连接。
- Console 查询实例状态时会保留正在启动中的 actor，不再把 `_started` 尚未置位但已绑定 OneBot 端口的实例误删为“未运行”，避免同进程旧监听泄漏后启动卡在端口清理阶段。
- OneBot transport 停止时会先等待 uvicorn 正常退出并释放监听端口，超时才取消任务，避免 Console 显示实例已停止但端口仍被同进程占用。
- 多 NapCat 实例启动前会要求配置数字 Bot QQ/self_id；端口仍是连接隔离的基础，self_id 校验只用于在 NapCat 端连错 URL 或旧连接未断干净时拒绝错误账号。
- `InstanceActor.start()` 会先完成传输层监听、标记 running 并返回，再启动 scheduler、maintenance、proactive 等后台任务，避免后台任务或外部 MCP 初始化拖慢 Console 的“运行”请求。
- Console 启动实例时只应用配置环境变量，不再刷新 MCP 工具定义；工具刷新保留在 Console 启动和设置保存路径，避免 Tavily 等外部 MCP 初始化超时拖慢每次实例启动。
- `启动仆仆控制台.bat` 会先检测 `http://127.0.0.1:8770/api/instances`，已有 Console 时直接打开页面，不再重复拉起第二个 Console；首次启动会等待健康检查成功，失败时提示查看 launcher 日志。
- 新增 actor 通用命令服务，`/help`、`/events`、`/facts`、`/tasks`、`/tidy`、`/recall`、`/debug`、`/silence` 等命令在 CLI 与 NapCat actor 中共用。
- CLI 切换为 `InstanceActor` 路径，CLI 与 NapCat actor 共用同一套消息处理、记忆写入和 batch review 流程。
- Scheduler 拆出 transport-neutral sender loop，定时任务通过 actor transport 投递；旧 NoneBot sender loop 已删除。
- 日志 sink 改为按实例路径缓存，actor 实例日志互不覆盖；无实例上下文时写入 `instances/_shared/logs`。
- wait-followup sender 捕获并恢复 `InstanceContext`，避免延迟回复任务在多实例场景中串上下文。
- Console 状态接口增加 `runtime` 字段，`pid` 返回控制台进程 pid。
- 增加 actor runtime、OneBot 消息解析、buffer 隔离、ProcessManager、wait-followup context 和 scheduler sender 相关测试。
- 使用本机 NapCat 实测 actor 模式：`cc3120f8` 连接 18081、`bd7dae8d` 连接 18082，两个实例都完成 reverse WebSocket 连接和 `get_login_info` echo 往返。
- 完成真实 NapCat 私聊端到端实测：`bd7dae8d` 在 actor 模式下收到 owner 私聊事件，调用聊天模型，并通过 OneBot `send_private_msg` 发出回复。
- 补充真实 NapCat 覆盖：分段私聊发送、图片消息解析、群聊 at 回复、开放群 arbiter 选中发言、`/silence on/off`、定时任务 sender、主动消息 sender、NapCat 断线重连。
- 补充两个真实 NapCat 账号互为 QQ 好友后的私聊互发实测：`cc3120f8` -> `bd7dae8d` 与 `bd7dae8d` -> `cc3120f8` 都能被对方 actor 收到，`user_id/self_id` 映射正确。
- 修复 actor 直接处理 OneBot event 时没有自动激活当前 `InstanceContext` 的问题，避免私聊白名单、owner 判断和实例配置读取落到错误上下文。
- 修复开放群 arbiter 在实例 `bot_id` 为空时身份不稳定的问题，优先使用 NapCat `self_id`；arbiter 会清理同 QQ 的陈旧 bot id。
- 增加 OneBot reverse WebSocket 集成测试，覆盖 actor 监听端口、模拟 NapCat 连接、`get_login_info` action/echo 响应和入站消息转发。

### 修复

- 修复 proactive 最近上下文丢失 `message.source` 的问题；定时任务、wait-followup 追问、实例主动消息现在会分别标成系统触发或实例主动发出，不再被模型误当成用户亲口发言。
- 聊天 prompt、语义召回 history 和 `/history` 展示统一使用 `message.source` 标注内部消息；定时任务触发记录不会再显示成用户发言。
- 清理 wait-followup 追问提示中的真实问号乱码，系统触发追问会明确告诉模型“这不是用户发言”。
- 修复 actor CLI 当前路径里的显示文案乱码，并改为使用当前实例名显示回复和思考状态。
- 修复旧实例库中 `person_facts` 存在重复行时，启动阶段创建唯一索引失败，导致 NapCat 看似一直停在等待连接的问题。
- `person_facts` 初始化迁移现在会在创建唯一索引前先按人物、对象、scope、fact key 去重，保留更新时间最新的一条。
- 增加重复 facts 迁移回归测试，避免旧库升级时再次阻塞实例启动。

### 运行时与实例上下文重构

- 新增显式 `InstanceContext`，实例目录、数据库、persona 和日志路径统一从当前实例上下文读取。
- 新增共享 runtime 层，集中管理 MCP 工具 runtime，为后续多实例单进程化做准备。
- 清理旧的实例路径环境变量依赖。
- 移除 `person_facts.legacy_session_id` 兼容字段，并在数据库初始化时自动迁移旧表结构。
- 修复只有 facts、没有聊天/摘要/事件线时维护流程不会扫描到该会话的问题。
- 更新 CLI、实例启动、日志、persona、配置、语义召回、工具 registry 等模块，使它们优先使用实例上下文。
- 扩充测试辅助工具和回归测试，覆盖实例上下文隔离、CLI 选实例、语义索引隔离、工具 runtime 复用和 facts schema 迁移。

### 验证

- 通过 `.\ForFun\Scripts\python.exe -m unittest discover tests`。
- 通过 `.\ForFun\Scripts\python.exe -m compileall -q pupu pupu_console tests`。
- 通过 `git diff --check`。
