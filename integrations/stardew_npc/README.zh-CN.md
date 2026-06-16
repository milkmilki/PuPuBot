# PuPu 星露谷完整 NPC 接入

这套接入会把仆仆做成星露谷里的正式 NPC，同时保留 PuPu 原来的记忆系统。

核心思路：

```text
星露谷里的仆仆 NPC
  -> SMAPI Mod 拦截右键对话
  -> POST 到 PuPu 本地 bridge
  -> pupu.agent.chat(session_id="owner")
  -> 返回回复，在游戏对话框显示
```

也就是说，游戏里的仆仆和 QQ/CLI 里的仆仆共享同一套：

- 记忆
- facts
- summary
- 好感度
- important events
- 定时任务

## 目录结构

星露谷接入文件都在独立目录里，不和 PuPu 主工程混在一起：

```text
integrations/stardew_npc/
  PuPuStardewNpcContentPack/  # Content Patcher 内容包
  PuPuStardewNpcMod/          # SMAPI C# Mod
  README.zh-CN.md
```

PuPu 主工程里只加了一个本地桥接服务：

```text
pupu/stardew_bridge.py
```

## 两个 Mod 分别做什么

### PuPuStardewNpcContentPack

这是 Content Patcher 内容包，负责让星露谷“认识仆仆这个 NPC”。

它提供：

- 正式 NPC 数据：`Data/Characters`
- 地图小人贴图：`Characters/PuPuBot_PuPu`
- 对话头像：`Portraits/PuPuBot_PuPu`
- 日常固定台词
- 婚后固定台词占位
- 日程
- 生日
- 礼物喜好
- 社交页/日历显示
- 可恋爱开关
- 节日站位
- spouse room / patio 占位数据

### PuPuStardewNpcMod

这是 SMAPI C# Mod，负责把游戏里的右键对话接到 PuPu。

它做的事：

- 找到正式 NPC `PuPuBot_PuPu`
- 玩家右键仆仆时打开输入框
- 把输入内容和游戏上下文发给 PuPu bridge
- 把 PuPu 的回复显示到游戏对话框

## 第一次安装流程

下面是我们实际跑通的流程。

### 1. 先启动一次原版星露谷

先从 Steam 正常打开一次星露谷，进到标题界面即可。

这一步用于确认游戏本体正常。

### 2. 安装 SMAPI

去 SMAPI 官网下载：

```text
https://smapi.io/
```

解压后运行：

```text
install on Windows.bat
```

安装完成后，如果你是 Steam 版，它会提示类似：

```text
"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\StardewModdingAPI.exe" %command%
```

把这一整行复制到 Steam 的启动选项里：

1. Steam 库里右键 `Stardew Valley`
2. 点 `属性`
3. 找到 `通用 -> 启动选项`
4. 粘贴整行，包括英文双引号和 `%command%`

### 3. 安装 Content Patcher

下载 Content Patcher：

```text
https://www.nexusmods.com/stardewvalley/mods/1915
```

解压后放到星露谷 Mods 目录：

```text
C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\ContentPatcher
```

### 4. 确认 SMAPI 和 Content Patcher 正常

从 Steam 启动星露谷。

SMAPI 控制台里应该能看到类似：

```text
[SMAPI] Loaded 3 mods:
[SMAPI]    Console Commands ...
[SMAPI]    Content Patcher ...
[SMAPI]    Save Backup ...
[SMAPI] Mods loaded and ready!
```

看到 `Content Patcher` 和 `Mods loaded and ready!` 就说明基础环境好了。

## 构建 PuPu 的 SMAPI Mod

### 1. 创建 stardewvalley.targets

在 PowerShell 里运行：

```powershell
'<Project><PropertyGroup><GamePath>C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley</GamePath></PropertyGroup></Project>' | Set-Content "$env:USERPROFILE\stardewvalley.targets" -Encoding UTF8
```

确认文件内容：

```powershell
Get-Content "$env:USERPROFILE\stardewvalley.targets"
```

应该看到：

```xml
<Project><PropertyGroup><GamePath>C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley</GamePath></PropertyGroup></Project>
```

如果你的星露谷不在这个目录，把 `GamePath` 改成你的实际安装目录。

### 2. 构建 Mod

在 PuPu 项目根目录运行：

```powershell
dotnet build .\integrations\stardew_npc\PuPuStardewNpcMod
```

成功时会看到：

```text
已成功生成。
0 个警告
0 个错误
```

构建成功后，`Pathoschild.Stardew.ModBuildConfig` 会自动把 `PuPuStardewNpcMod` 复制到星露谷 Mods 目录：

```text
C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\PuPuStardewNpcMod
```

## 安装 Content Pack

`PuPuStardewNpcContentPack` 不是 C# 项目，不需要构建，需要复制到 Mods 目录。

可以手动复制：

```text
integrations\stardew_npc\PuPuStardewNpcContentPack
```

到：

```text
C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\PuPuStardewNpcContentPack
```

也可以在 PowerShell 里运行：

```powershell
$source = Resolve-Path -LiteralPath 'integrations\stardew_npc\PuPuStardewNpcContentPack'
$dest = 'C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\PuPuStardewNpcContentPack'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Get-ChildItem -LiteralPath $source.Path -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force }
```

最后 Mods 目录里应该有：

```text
Mods/
  ContentPatcher/
  PuPuStardewNpcContentPack/
  PuPuStardewNpcMod/
```

## 启动 PuPu 桥接服务

每次进游戏前，先在 PuPu 项目根目录启动：

```powershell
.\scripts\run_stardew_npc_bridge.bat
```

或者：

```powershell
.\.venv\Scripts\python.exe -m pupu.stardew_bridge
```

成功时会看到：

```text
[pupu][stardew-npc] bridge started http://127.0.0.1:18787 session=owner
```

这个窗口要保持打开。

健康检查：

```text
http://127.0.0.1:18787/health
```

正常返回：

```json
{"ok": true, "service": "pupu-stardew-npc-bridge"}
```

## 启动游戏后应该看到什么

从 Steam 启动星露谷，SMAPI 控制台应该出现：

```text
[SMAPI] Loaded 4 mods:
[SMAPI]    Content Patcher ...
[SMAPI]    PuPu Stardew NPC 0.1.0 by PuPuBot ...

[SMAPI] Loaded 1 content packs:
[SMAPI]    PuPu Stardew NPC Content 0.1.0 by PuPuBot | for Content Patcher ...

[SMAPI] Mods loaded and ready!
```

如果没有红色 Content Patcher 报错，说明仆仆 NPC 内容包加载成功。

`No update keys` 提示可以忽略，那只是说本地 Mod 没写自动更新地址。

## 游戏内使用

默认仆仆的 home point 是：

```text
Farm tile=(64, 15)
```

她没有新房子，目前是把农场上的这个点当作 home point。

她会根据：

```text
PuPuStardewNpcContentPack/data/schedule.json
```

在农场、城镇、海滩、森林、商店等地方移动。

靠近仆仆后：

- 右键仆仆：打开 PuPu 输入框
- Enter：发送
- Esc：取消
- 回复会显示成星露谷对话框

也可以在 SMAPI 控制台测试：

```text
pupu 仆仆你在吗
```

## PuPu 侧配置

`pupu.yaml` 里可以配置：

```yaml
stardew:
  host: 127.0.0.1
  port: 18787
  session_id: owner
  token: ""
  reply_hint: ""
```

说明：

- `stardew.port=18787`：PuPu bridge 监听端口。
- `stardew.session_id=owner`：共享你现在和仆仆的主记忆。
- `stardew.token`：可选密钥。如果这里设置了，Mod 配置里也要填同一个 `Token`。
- `stardew.reply_hint`：可选回复风格提示。

## Mod 配置

第一次运行后，SMAPI 会生成：

```text
C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\PuPuStardewNpcMod\config.json
```

示例：

```json
{
  "BridgeUrl": "http://127.0.0.1:18787/chat",
  "Token": "",
  "SessionId": "owner",
  "NpcInternalName": "PuPuBot_PuPu",
  "NpcName": "仆仆",
  "PortraitAssetName": "Portraits/PuPuBot_PuPu",
  "TalkButton": "MouseRight",
  "InteractDistanceTiles": 2.0,
  "RequestTimeoutSeconds": 180,
  "IncludeGameContext": true,
  "ShowDialogueBox": true,
  "ShowHudNotification": false
}
```

注意：

- 如果你把 PuPu bridge 端口改了，`BridgeUrl` 也要同步改。
- `NpcInternalName` 必须和内容包里的 `PuPuBot_PuPu` 一致。
- `SessionId=owner` 表示和 QQ/CLI 共用同一套仆仆记忆。

## 仆仆知道自己在星露谷里吗

知道。

SMAPI Mod 会把游戏上下文发给 PuPu bridge，桥接层会把用户消息包装成类似：

```text
[星露谷NPC | 对话对象=仆仆; 玩家=...; 农场=...; 地点=Farm; 季节=spring; 日期=3; 年份=1; 时间=930; 天气=clear; hearts=0] 你说的话
```

PuPu 仍然使用原来的主提示词、persona、facts、summary，只是额外知道这是星露谷场景。

## 资源位置

完整 NPC 资源放在：

```text
integrations/stardew_npc/PuPuStardewNpcContentPack/assets/
```

当前资源：

```text
pupu_sprites.png
pupu_portraits.png
```

规格：

```text
pupu_sprites.png    64x128，16 帧，每帧 16x32
pupu_portraits.png  128x192，6 帧，每帧 64x64，2列x3行
```

换图时保持文件名和尺寸不变，通常不需要改其他配置。

## NPC 数据位置

核心内容包文件：

```text
PuPuStardewNpcContentPack/content.json
PuPuStardewNpcContentPack/data/dialogue.json
PuPuStardewNpcContentPack/data/marriage_dialogue.json
PuPuStardewNpcContentPack/data/schedule.json
```

用途：

- `content.json`：NPC 注册、生日、礼物喜好、恋爱开关、节日站位等。
- `data/dialogue.json`：原生固定日常台词，作为兜底。
- `data/marriage_dialogue.json`：婚后台词占位。
- `data/schedule.json`：日程。

真正动态接入 PuPu 的地方不是 `dialogue.json`，而是：

```text
PuPuStardewNpcMod/ModEntry.cs
pupu/stardew_bridge.py
```

## 常见问题

### SMAPI 里只看到 Content Patcher，没看到 PuPu Mod

先重新构建：

```powershell
dotnet build .\integrations\stardew_npc\PuPuStardewNpcMod
```

确认生成后自动复制到了：

```text
C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\PuPuStardewNpcMod
```

### 只看到 PuPu Mod，没看到 PuPu Content Pack

说明内容包没复制过去。

复制：

```text
integrations\stardew_npc\PuPuStardewNpcContentPack
```

到：

```text
C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\PuPuStardewNpcContentPack
```

### Content Patcher 报 Data/Characters 格式错误

说明 `content.json` 里某个字段不符合当前星露谷版本的数据格式。

我们之前遇到过 `SpousePatio.SpriteAnimationFrames` 的格式错误，已经移除了这个可选字段。遇到新报错时，看报错里的 `Path '...'`，定位到 `content.json` 对应字段修。

### 右键仆仆后显示连不上 bridge

先确认 PuPu bridge 开着：

```powershell
.\scripts\run_stardew_npc_bridge.bat
```

再确认 Mod 配置：

```json
"BridgeUrl": "http://127.0.0.1:18787/chat"
```

### 8787 和 18787

旧版曾经用过 `8787`，现在默认已改成：

```text
18787
```

如果你之前生成过旧的 `config.json`，需要手动把 `BridgeUrl` 改成：

```text
http://127.0.0.1:18787/chat
```

## 当前限制

- 仆仆目前没有新建专属房子。
- 她的 home point 是农场 `Farm (64,15)`。
- spouse room / patio 目前是占位，使用游戏自带资源。
- `dialogue.json` 是固定兜底台词；真正动态聊天走 PuPu bridge。

后续可以继续做：

- 仆仆专属小屋
- 专属房间地图
- 更完整的节日对话
- 事件剧情
- 动态主动打招呼
- 根据游戏事件主动说话
