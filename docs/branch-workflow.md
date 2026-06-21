# 分支协作约定

PuPuBot 当前使用 `main` + `siri` 的双分支工作流。这个文档记录给后续维护者和 agent 的默认操作规则，避免后端修复和前端集成分散在错误分支上。

## 分支职责

- `main` 是后端事实来源。所有 `pupu/`、`pupu_console/`、`tests/`、配置加载、实例运行时、记忆系统、CLI、NapCat、Console API 等后端改动，都应先落在 `main`。
- `siri` 是前端集成分支。它在 `main` 后端基础上增加 `desktop/pupu-siri/` Tauri 桌宠前端。
- `siri` 可以包含后端代码，但这些后端代码应来自 `main` 的合并或 cherry-pick，而不是直接在 `siri` 上首发实现。

## 默认开发流程

后端改动：

1. `git switch main`
2. 实现后端修复或能力。
3. 更新 `CHANGELOG.md`，说明行为变化、测试覆盖或交接注意点。
4. 运行相关测试。
5. 提交到 `main`。
6. `git switch siri`
7. 将 `main` 的改动同步到 `siri`，优先使用 `git merge main`；只有明确只需要某个提交时才用 cherry-pick。

前端改动：

1. `git switch siri`
2. 修改 `desktop/pupu-siri/`。
3. 如前端依赖新的后端能力，先确保该后端能力已经在 `main` 落地并同步进 `siri`。
4. 更新 `CHANGELOG.md` 或 `desktop/pupu-siri/README.md` 中对应说明。
5. 运行前端和必要的后端契约测试。

## Agent 接手规则

- 开始修改前先看 `git status --short --branch`，确认当前分支和未提交内容。
- 如果任务是后端修复，即使当前在 `siri`，也应先切到 `main`；不要直接在 `siri` 首发后端改动。
- 如果已经误在 `siri` 上做了后端未提交改动，应先 stash 指定文件，切到 `main` 后应用，再提交到 `main`，最后同步回 `siri`。
- 不要把 `desktop/` 目录提交到 `main`；`desktop/pupu-siri/` 只属于 `siri`。
- 每次改代码都要更新 `CHANGELOG.md`，让后续 agent 能知道这次改了什么、为什么改、如何验证。
- 提交前只 stage 本次任务相关文件，排除实例数据、日志、SQLite、`pupu.yaml` 和无关前端/后端文件。

## 同步方式选择

优先选择：

```powershell
git switch siri
git merge main
```

只有在以下情况使用 cherry-pick：

- `siri` 只需要 `main` 上的某一个小修复。
- `main` 上还有暂时不想进入 `siri` 的其他提交。
- 用户明确要求只同步某个提交。

## 当前约定背景

`siri` 的桌宠前端通过本地 PuPu Console 的 HTTP/WebSocket API 工作，不直接 import Python。后端运行时、proactive、CLI、NapCat、记忆系统和 Console API 都仍由 `main` 维护。保持这个边界可以让前端分支专注桌面体验，同时避免后端修复在两个分支里重复发散。
