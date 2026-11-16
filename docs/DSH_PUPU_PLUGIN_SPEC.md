# PuPu 原生 DSH 插件规格

日期：2026-10-19。状态：产品与技术提案，未实现或发布。

## 1. 目标与用户流程

用户希望将 PuPu 做成类似社区角色扮演插件的原生 DSH 插件，符合安装及市场索引规范，提供 UI，并保留 PuPu 的记忆系统和可导入数据。

日常使用目标：安装插件 → 导入角色及记忆包 → 点击角色开始聊天。用户无需手工配置 MCP 工具、角色 ID 或数据库路径。

首版假设：先验证用户当前的 Windows Host，面向官方 DSH Web UI；Desktop 只有完成实测后才列入兼容声明。原 PuPu 保持可运行，真实记忆与插件软件分开发行。安装者可以不导入任何真实记忆，自行创建角色。

与 [记忆迁移规格](MEMORY_MCP_MIGRATION_SPEC.md) 共用同一个记忆核心：人物事实、关系、事件链、摘要、证据和审计保留；MCP 是向其他宿主提供的通用入口，DSH 用户主要通过原生插件操作。

## 2. UI 与行为

| 入口 | 用户操作 | 结果与限制 |
| --- | --- | --- |
| 角色管理 | 新建、编辑名字、人设、头像、查看绑定记忆库 | 角色设定与记忆分别保存；编辑人设不覆盖已有经历 |
| 导入向导 | 选择 `.pupumem` 文件 → 查看角色、记忆计数及冲突 → 导入 | 导入前预检；默认新建独立角色库，不直接写原 PuPu 实例 |
| 聊天输入区 | 选择“普通助手”或具体角色，显示当前角色名 | 空会话可选择；开聊后固定绑定，切换角色进入新的角色会话 |
| 角色详情 | 点击“开始聊天”或查看已有会话 | 跨会话沿用该角色的记忆，角色之间的聊天上下文分别隔离 |
| 记忆管理 | 搜索事实、关系、事件和摘要；展开证据与事件时间线 | 保留来源、时间、置信度；显示本次命中缘由 |
| 数据管理 | 导出完整迁移包或精选种子包、创建备份、查看导入报告 | 软件包不附带私人记忆；导出数据仍归原人物所有 |
| 运行状态 | 查看记忆引擎、待整理批次、索引与错误状态 | 失败提供具体修复动作，不能把未运行或失败显示为已完成 |

普通助手默认不注入角色人设、不召回角色私人记忆、不向角色库写入。只在正文中提到另一角色不会改变当前绑定。

使用 DSH 的组件、主题 token 和扩展插槽。界面适配浅色、深色与窄屏；输入有标签，按钮支持键盘，导入过程包含进度、错误、重试和清楚的完成状态。人设和记忆正文按文本呈现，不执行导入包中的 HTML/脚本。

## 3. 原生插件结构

```text
dsh-pupu（发行名暂定）
  ├─ Host 插件：角色与会话绑定、宿主模型 review、记忆引擎生命周期
  ├─ Client 插件：角色管理、输入区选角、记忆浏览、导入导出
  └─ PuPu 记忆核心：SQLite、事件链、证据、审计、语义召回
       └─ 通用 MCP 入口：供其他兼容宿主使用
```

- 使用 TypeScript/React 接入 DSH Host/Client 扩展，不修改 DSH 源码或依赖 DOM 选择器挂载。
- Host 暴露类型化的远程接口；Client 不直接接触 SQLite 或本机任意路径。
- 模型侧工具只绑定当前会话的角色作用域，管理界面拥有单独的管理操作入口。不能让模型通过传入任意 `character_id` 访问其他角色。
- 会话、召回缓存、摘要与后台 review 都携带角色作用域；不存在全局共享的“当前扮演人物”。
- 普通文本回复和消息身份使用宿主持久事件，不提取思维链或工具流为角色经历。
- 插件卸载时清理界面、监听器、定时器与自管子进程；保留用户记忆数据。删除角色与物理删除记忆是两个独立操作。

### 记忆引擎的发行约束

现有核心是 Python，首版优先复用已有行为，不能为界面方便而直接重写成一套精简记忆数据库。

要实现没有 Python 环境的机器也能使用，须为声明支持的平台提供受插件管理的预构建引擎，或另行验证等价的原生实现。首次启动在 UI 中报告引擎检查结果。依赖用户本机 Python 的开发版本必须明确标注，不能宣称为零配置正式安装包。

引擎不得以用户工作目录推导数据库位置；数据放在显式的插件数据根目录。启停幂等，DSH 探测或重载不触发隐式导入和破坏性维护。模型推理复用宿主模型配置；embedding 单独选配，缺失时可使用本地检索。

## 4. 官方规范核查

依据 2026-10-19 查询的官方文档；具体兼容范围仍须以实际测试版本为准。

| 要求 | 本插件需要交付 |
| --- | --- |
| 标准 bundle | `package.json` 的 `dsh.bundle` 指向随包提供的 `cordis.patch.yml`，使用插入插件行的补丁格式 |
| 浏览器模块 | `dsh.client` 声明与构建后的 `./client` 导出；采用 DSH client loader 要求的 lazy-CJS factory 格式，普通 ESM 文件不能冒充客户端产物 |
| 原生 UI | 通过 `ctx.slots` 和公开的 Client/Remote 服务挂载，并跟随插件生命周期移除 |
| 依赖与兼容 | 共享宿主实例的 DSH 包列入正确的 peer/dev dependencies；`engines.dsh` 只声明验证过的版本范围 |
| 可安装产物 | npm 或预构建 `.tgz` 包含运行所需 Host/Client 产物和已声明平台的引擎；不依赖 PuPu 原仓库或 DSH 源码仓库 |
| 可维护性 | README、许可证、版本变更、数据路径、备份恢复、禁用卸载说明、清楚的平台和 DSH 版本支持范围 |
| 市场索引 | UTF-8 JSON，`format: 1`，有效包名与版本；提供标题、说明、图标及准确的 npm/git 安装来源，兼容信息与安装包一致 |

官方来源：

- [Package and install a plugin](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/publish)
- [Web Client Slots](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/slots)
- [Live configuration forms / Client distribution](https://deepseek-harness.github.io/deepseek-harness/en/reference/cookbook/adding-a-settings-card)
- [Plugin markets](https://deepseek-harness.github.io/deepseek-harness/en/guide/plugin-market)

DSH 文档所述市场机制是可配置的第三方索引；符合格式不等于通过某个市场的审核或获得官方认证。首版交付可安装包和待发布索引资料；仓库地址、作者、包名归属与真实发布版本落实后才能填写正式市场索引。不能用虚构地址或不存在的 npm 版本声称已上架。

## 5. 结构、接口风格与预期命令

以下为实现目标，当前尚无对应安装包或已验证命令：

```text
integrations/dsh-pupu/
  package.json
  cordis.patch.yml
  src/host/           # 会话绑定、Remote、引擎管理
  src/client/         # 原生 UI
  src/shared/         # 输入输出契约
  tests/              # 核心集成、作用域、生命周期
  market/             # 图标与市场元数据模板
  README.md
```

接口先定义角色、作用域和返回类型，避免前端使用无类型的任意命令：

```typescript
interface ConversationBinding {
  readonly conversationId: string
  readonly mode: 'assistant' | 'roleplay'
  readonly characterId: string | null
  readonly scopeVersion: number
}
```

预期提供 `typecheck`、`test`、`build`、`check-package` 脚本，并验证：

```powershell
cd integrations/dsh-pupu
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm test
pnpm run build
pnpm run check-package
pnpm pack
# 使用生成的实际 tgz 文件名；在独立测试 profile 中安装
dsh plugin --profile pupu-test add ./dsh-pupu-0.1.0.tgz
dsh --profile pupu-test --dump-config
```

测试 profile 启动所需 Web composition 与端口遵循所验证 DSH 版本的配置；不能假定任意新建 profile 已包含 Web UI。测试在独立 DSH_HOME 中完成，不覆盖用户默认 profile。

## 6. 实施顺序与验收

1. 验证最小原生 bundle 能安装、显示并卸载一个 UI 入口，确定 DSH 版本和 Client 构建格式。
2. 完成角色管理和会话绑定，验证普通助手、角色 A、角色 B 并行时不串身份和上下文。
3. 接入 PuPu 记忆核心，实现导入预检与恢复、事实和事件浏览、来源证据追踪。
4. 接通自动召回与 review，验证跨会话记忆、失败恢复、重启幂等及延迟任务作用域。
5. 在干净环境验证预构建引擎和 `.tgz` 安装，核对市场索引格式、真实版本及兼容声明。

每一步建立能检查真实行为的测试；UI 必须在实际 DSH 页面中检查交互、控制台和窄屏。只通过类型检查或静态 UI 预览不能宣称插件已兼容。

验收成功时，安装者应能完全通过界面创建角色、导入记忆、选择角色开始聊天、查看已有事实和事件，并确认普通聊天不进入角色记忆。升级、禁用与卸载不得删除记忆。原 PuPu 的相关回归必须通过。

## 7. 操作边界

- 始终：优先复用已验证的记忆行为，使用公开扩展点，隔离角色与会话，先备份再导入，按实际测试填写支持范围。
- 首版范围：角色与记忆管理 UI、可导入数据包、自动记忆、原生 DSH 安装包和市场发布资料；不加入桌宠、语音或 QQ 接入。
- 另行确认：公开发布 npm/GitHub 包、向具体市场提交，以及公开分发真实记忆。当前请求不视为授权公开私人数据。
- 尚待决策：面向 DSH 的产品交付以原生插件为主，MCP 保留为通用接口；实施前需确认此规格，之后首次真实迁移再选择实例和数据包模式。
