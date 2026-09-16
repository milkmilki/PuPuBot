# NapCat QQ 调试指南

PuPu 现在只保留两种入口：CLI 和 NapCat。QQ 侧使用 PuPu 自带的轻量 OneBot v11 reverse WebSocket actor，不再依赖 NoneBot，也不再支持 QQ 官方 Bot 路径。

## 架构

```text
QQ 客户端 / NapCat
        |
        | reverse WebSocket
        v
PuPu InstanceActor
  ws://127.0.0.1:<实例端口>/onebot/v11/ws
        |
        v
pupu.agent.chat()
```

每个实例监听自己的端口。端口写在 `instances/<id>/instance.json` 的 `port` 字段里，也会在 Console 实例列表里显示。

## NapCat 配置

在 NapCat 的 OneBot v11 网络配置里添加反向 WebSocket：

```json
{
  "network": {
    "websocketClients": [
      {
        "enable": true,
        "url": "ws://127.0.0.1:18081/onebot/v11/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": ""
      }
    ]
  }
}
```

要点：

- URL 必须带 `/onebot/v11/ws`。
- 端口必须等于实例端口，例如 `18081` 或 `18082`。
- `messagePostFormat` 建议为 `array`，这样图片、at、文本段能稳定解析。
- 如果 NapCat 里配置了 `token`，在 `pupu.yaml` 的 `napcat.access_token` 填同一个值；否则两边都留空。

## 启动顺序

推荐顺序：

1. 启动 PuPu Console 或 `python start.py`。
2. 启动实例，看到日志：

```text
[PuPu Actor] NapCat reverse WebSocket listening at ws://127.0.0.1:<port>/onebot/v11/ws
[PuPu QQ] mode: NapCat actor (OneBot v11)
```

3. 启动或重连 NapCat。
4. 成功后 PuPu 日志会出现：

```text
[pupu][actor] NapCat connected self_id=<机器人QQ>
```

NapCat 通常会自动重连，所以顺序不是绝对要求；但排查时先启动 PuPu 更直观。

## 常见问题

### 1. PuPu 停在 waiting for NapCat connection

检查：

- Console 里该实例是否已经启动。
- NapCat 配置的 URL 端口是否等于实例端口。
- URL 是否带 `/onebot/v11/ws`。
- 本机防火墙是否拦截。

Windows 可用：

```powershell
netstat -ano | findstr ":18081"
```

应能看到 PuPu 监听该端口。

### 2. NapCat 日志收到 QQ 消息，但 PuPu 没反应

检查：

- NapCat 是否真的连上 PuPu actor，而不是只连了别的 OneBot 服务。
- `messagePostFormat` 是否为 `array`。
- 私聊白名单：`instance.json` 中 `private_reply_mode` 为 `owner_only` 时，只有 `owner_ids` 会回复。
- 群聊默认需要 at；只有 `open_groups` 里的开放群会观察全量消息。
- `/` 开头的消息会被命令路由截断，不会进入聊天模型。

### 3. 群聊没人回复

普通群聊：需要 at 当前机器人。

开放群：需要在实例里配置：

```json
{
  "open_groups": ["1103489921"],
  "bot_id": "bot_1",
  "peer": {
    "bot_id": "bot_2",
    "name": "另一个实例",
    "qq": "123456789",
    "persona_brief": "简短身份描述"
  }
}
```

开放群还需要启动 arbiter 服务。Console 里有仲裁器启动按钮；日志正常时会看到 actor 向 arbiter observe 群消息。

### 4. silence 开关

在开放群里发送：

```text
/silence on
/silence off
```

- `on`：当前实例在该群保持沉默，并停止连接 arbiter。
- `off`：恢复观察群消息并允许连接 arbiter。

### 5. 定时任务和主动消息没发出去

确认：

- 当前实例是 `qq_mode=napcat`，且 NapCat 已连接。
- `owner_ids` 至少有一个数字 QQ，用于 owner 私聊投递。
- `instance.json.proactive_enabled` 为 `true`。
- 日志中没有 `NapCat is not connected` 或 OneBot action timeout。

## 快速验证

1. 私聊机器人发送 `/help`，应返回命令列表。
2. 私聊发送普通文本，应进入模型并回复。
3. 群聊 at 机器人发送 `/help`，应返回命令列表。
4. 开放群里两个实例同时在线时，观察 arbiter 日志确认只选择一个实例发言。
