# 更新日志

## 2026-06-21

### 修复

- 修复旧实例库中 `person_facts` 存在重复行时，启动阶段创建唯一索引失败，导致 `pupu_plugin` 加载失败、NapCat 看似一直停在等待连接的问题。
- `person_facts` 初始化迁移现在会在创建唯一索引前先按人物、对象、scope、fact key 去重，保留更新时间最新的一条。
- 增加重复 facts 迁移回归测试，避免旧库升级时再次阻塞实例启动。

### 运行时与实例上下文重构

- 新增显式 `InstanceContext`，实例目录、数据库、persona、日志和 memU 路径统一从当前实例上下文读取。
- 新增共享 runtime 层，集中管理 MCP 工具 runtime 和 memU runtime，为后续多实例单进程化做准备。
- 清理旧的实例路径环境变量依赖，控制台启动子进程时不再注入旧路径变量。
- 移除 `person_facts.legacy_session_id` 兼容字段，并在数据库初始化时自动迁移旧表结构。
- 修复只有 facts、没有聊天/摘要/事件线时维护流程不会扫描到该会话的问题。
- 更新 CLI、实例启动、日志、persona、配置、memU、工具 registry 等模块，使它们优先使用实例上下文。
- 扩充测试辅助工具和回归测试，覆盖实例上下文隔离、CLI 选实例、memU runtime 隔离、工具 runtime 复用和 facts schema 迁移。

### 验证

- 通过 `.\ForFun\Scripts\python.exe -m unittest discover tests`。
- 通过 `.\ForFun\Scripts\python.exe -m compileall -q pupu pupu_console plugins tests`。
- 通过 `git diff --check`。
