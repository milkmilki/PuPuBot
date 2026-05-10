# QQ Bot 双模式调试指南

## 架构概览

```
模式1: NapCat (OneBot v11)
  QQ Server → NapCat(QQ客户端) --反向WS--> bot.py(NoneBot2 FastAPI服务端)

模式2: QQ 官方机器人
  QQ Server <--WS+HTTP--> bot.py(NoneBot2 主动连接QQ服务器)
```

## 模式1: NapCat (OneBot v11)

### 关键配置

| 组件 | 配置文件 | 作用 |
|------|----------|------|
| bot.py driver | `.env.qq` + `bot.py` | `driver="~fastapi"`, HOST=0.0.0.0, PORT=8081 |
| NapCat 网络 | `NapCat/config/onebot11_<QQ号>.json` | `websocketClients` 反向WS连接地址 |

### 踩过的坑

#### 1. driver 必须用 `~fastapi`，不能用 `~websockets`

- `~websockets` 是**正向WS客户端**驱动，只能主动连别人
- `~fastapi` 才是**服务端**驱动，能监听端口接受 NapCat 的反向WS连接
- 症状：bot.py 显示 `Application startup completed` 但 `netstat` 看不到 8081 端口监听
- 需要安装：`pip install nonebot2[fastapi]`（会装 fastapi + uvicorn）

#### 2. NapCat 的 `websocketClients` 不能为空

NapCat 默认配置所有网络数组都是空的，必须手动添加反向WS连接：

```json
// onebot11_<QQ号>.json
{
  "network": {
    "websocketClients": [
      {
        "enable": true,
        "url": "ws://127.0.0.1:8081/onebot/v11/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": ""
      }
    ]
  }
}
```

- 症状：NapCat 日志能看到 `接收 <- 私聊` 但没有任何转发行为
- 也可以通过 NapCat WebUI (`http://localhost:6099`) 配置

#### 3. 反向 WS 的 URL 必须带 `/onebot/v11/ws`，且端口与仆仆一致

- 错误示例：`ws://localhost:8081`（缺路径，NoneBot 不会按 OneBot v11 握手）
- 正确示例：`ws://127.0.0.1:8081/onebot/v11/ws`（端口改成你 `.env.qq` 里 `PORT=` 的值；多实例看该实例目录下的 `.env.qq`）
- 若 NapCat 里 `token` 非空，仆仆端 `.env.qq` 需配置相同的 `ONEBOT_ACCESS_TOKEN`；否则把 NapCat 的 `token` 留空 `""`

### 排查流程

1. **bot.py 启动后检查端口**：`netstat -ano | grep ":8081 "` 必须看到 LISTENING
2. **NapCat 日志看连接状态**：成功会显示 WebSocket 连接成功，失败会 `ECONNREFUSED`
3. **先启动 bot.py，再启动 NapCat**（NapCat 会自动重连，顺序不严格但方便排查）

### 开放群与中央仲裁器

开放群用于“三人群”（用户 + bot_1 + bot_2）这类场景：白名单群不再要求 `to_me()`，两个 PuPu 实例都会读到群内全部消息。每轮防抖结束后，实例会请求控制台的 `POST /api/group_arbitrate`，由共享仲裁器决定本轮 `speaker` 是 `bot_1`、`bot_2` 还是 `none`。

实例配置字段在 `instance.json`：

```json
{
  "open_groups": ["1103489921"],
  "bot_id": "bot_1",
  "arbiter_url": "http://127.0.0.1:18079/api/group_arbitrate",
  "peer": {
    "bot_id": "bot_2",
    "name": "小白",
    "qq": "123456789",
    "persona_brief": "另一个仆仆实例的简短人设"
  },
  "debounce_seconds_open_group": 35
}
```

要点：

1. 两个实例的同一个群号都要写进 `open_groups`。
2. `bot_id` 必须稳定且互不相同；`peer.qq` 用于显式 `@` 直通。
3. 控制台必须在运行，因为仲裁器 API 挂在 `pupu_console.server` 上；仲裁结果存到 `instances/_shared/arbiter.db`。
4. 仲裁器超时、LLM 失败或控制台不可达时，实例默认静默；显式 `@` 对应 bot 时可绕过 LLM。
5. 模型若要真正 @ 对方 QQ，输出 `<at qq="123456789"/>`，发送层会转换成 OneBot `at` 消息段。

---

## 模式2: QQ 官方机器人

### 关键配置

| 组件 | 配置文件 | 作用 |
|------|----------|------|
| bot.py driver | `bot.py` | `driver="~httpx+~websockets"` (客户端主动连QQ服务器) |
| 凭证 | `config.json` | `qq_app_id` + `qq_app_secret` |
| intent | `bot.py` 中 `qq_bots` 参数 | 控制订阅哪些事件类型 |

### 踩过的坑

#### 1. 必须配置 `intent.c2c_group_at_messages: True`

nonebot-adapter-qq 的 `Intents` 大部分默认关闭，其中 `c2c_group_at_messages` 控制：
- C2C 私聊消息（`C2CMessageCreateEvent`）
- 群@机器人消息（`GroupAtMessageCreateEvent`）

不开这个 intent = 收不到这两类消息。

```python
qq_bots=[{
    "id": app_id,
    "token": "",
    "secret": app_secret,
    "intent": {
        "c2c_group_at_messages": True,
    },
}]
```

- 症状：bot 正常启动连接成功，但收不到任何消息，没有报错
- `token` 字段留空字符串即可，实际认证走 OAuth2（id + secret）

#### 2. Intents 默认值参考

| Intent | 默认 | 说明 |
|--------|------|------|
| `guilds` | True | 频道事件 |
| `guild_members` | True | 频道成员事件 |
| `at_messages` | True | 频道@消息 |
| `direct_message` | False | 频道私信 |
| `c2c_group_at_messages` | **False** | **C2C私聊 + 群@消息** |
| `guild_messages` | False | 频道全量消息（需申请） |

### 排查流程

1. **检查凭证**：AppID 和 AppSecret 是否正确（QQ开放平台获取）
2. **检查 intent**：是否开启了需要的事件订阅
3. **看 bot.py 日志**：成功会显示 WebSocket 连接到 QQ 服务器，失败会有 401/403 错误
4. **QQ开放平台**：确认机器人已上线/沙箱模式，相关权限已开通
