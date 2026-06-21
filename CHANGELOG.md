# 更新日志

## 2026-06-21

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
- 补充 hook 生命周期测试的实例上下文与 memU 隔离，单独运行 `tests.test_hooks` 不再误触真实长记忆索引。

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
- 聊天 prompt、memU recall history 和 `/history` 展示统一使用 `message.source` 标注内部消息；定时任务触发记录不会再显示成用户发言。
- 清理 wait-followup 追问提示中的真实问号乱码，系统触发追问会明确告诉模型“这不是用户发言”。
- 修复 actor CLI 当前路径里的显示文案乱码，并改为使用当前实例名显示回复和思考状态。
- 修复旧实例库中 `person_facts` 存在重复行时，启动阶段创建唯一索引失败，导致 NapCat 看似一直停在等待连接的问题。
- `person_facts` 初始化迁移现在会在创建唯一索引前先按人物、对象、scope、fact key 去重，保留更新时间最新的一条。
- 增加重复 facts 迁移回归测试，避免旧库升级时再次阻塞实例启动。

### 运行时与实例上下文重构

- 新增显式 `InstanceContext`，实例目录、数据库、persona、日志和 memU 路径统一从当前实例上下文读取。
- 新增共享 runtime 层，集中管理 MCP 工具 runtime 和 memU runtime，为后续多实例单进程化做准备。
- 清理旧的实例路径环境变量依赖。
- 移除 `person_facts.legacy_session_id` 兼容字段，并在数据库初始化时自动迁移旧表结构。
- 修复只有 facts、没有聊天/摘要/事件线时维护流程不会扫描到该会话的问题。
- 更新 CLI、实例启动、日志、persona、配置、memU、工具 registry 等模块，使它们优先使用实例上下文。
- 扩充测试辅助工具和回归测试，覆盖实例上下文隔离、CLI 选实例、memU runtime 隔离、工具 runtime 复用和 facts schema 迁移。

### 验证

- 通过 `.\ForFun\Scripts\python.exe -m unittest discover tests`。
- 通过 `.\ForFun\Scripts\python.exe -m compileall -q pupu pupu_console tests`。
- 通过 `git diff --check`。
