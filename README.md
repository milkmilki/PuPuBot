# 仆仆 (PuPu)

一个有性格的 AI 聊天伙伴，基于 Claude API 构建。支持 QQ 机器人和终端两种交互方式。

## 她是谁

仆仆是一个假小子风格的 AI 角色——爽朗、直接、嘴硬心软。她有一套好感度系统，会根据你们的互动逐渐改变态度：从冷淡的陌生人，到会主动找你聊天的铁哥们。

## 功能

- **好感度系统** — 0-100 分，5 个关系阶段（陌生/认识了/熟了/好朋友/铁哥们），每个阶段说话风格不同
- **关系记忆** — 记住你们一起经历的事，并在对话中自然地提起
- **联网能力** — 可以搜索网页（DuckDuckGo）和抓取网页内容
- **QQ 机器人** — 支持 NapCat（OneBot v11）和 QQ 官方机器人两种接入方式
- **终端模式** — 不需要 QQ 也能直接在命令行里聊天

## 快速开始

### 1. 环境准备

```bash
# 创建并激活虚拟环境
python -m venv ForFun
source ForFun/Scripts/activate   # Windows Git Bash
# ForFun\Scripts\activate.bat    # Windows CMD

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

复制 `.env` 文件并填写 API 密钥：

```
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_BASE_URL=https://api.anthropic.com   # 或你的代理地址
```

### 3. 运行

```bash
python start.py
# 或双击 启动仆仆.bat
```

启动后选择模式：
- **[1] 终端聊天** — 直接在命令行里和仆仆聊天
- **[2] QQ - NapCat** — 需要另外安装 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)，配置反向 WebSocket 连接到 `ws://127.0.0.1:8081/onebot/v11/ws`
- **[3] QQ - 官方** — 需要在 [QQ 开放平台](https://q.qq.com/) 注册机器人，获取 AppID 和 AppSecret

终端和 QQ 私聊（owner）共享同一个仆仆——同一份聊天记录、好感度和记忆。

### 4. 聊天命令

| 命令 | 说明 |
|------|------|
| `/score` | 查看好感度和最近事件 |
| `/history` | 查看最近聊天记录 |
| `/quit` | 退出（仅终端模式） |

## 项目结构

```
ForFunny/
├── start.py            # 统一入口（终端 / QQ 机器人）
├── 启动仆仆.bat         # 双击启动
├── config.json         # 运行时配置（模式、QQ 凭证、管理员）
├── .env                # API 密钥（不要提交！）
├── .env.qq             # NoneBot 服务器配置
├── requirements.txt    # Python 依赖
├── pupu/               # 核心逻辑
│   ├── agent.py        # 对话主循环，Claude API 调用
│   ├── persona.py      # 人设 prompt 和好感度判定 prompt
│   ├── memory.py       # SQLite 持久化（消息、好感度、事实、摘要）
│   ├── tools.py        # 工具兼容层（registry facade）
│   ├── tooling/        # MCP 风格工具系统（registry + servers）
│   └── cli.py          # 终端界面
├── plugins/
│   └── pupu_plugin.py  # NoneBot 插件，QQ 消息处理
├── data/
│   └── pupu.db         # SQLite 数据库（自动创建，已 gitignore）
├── docs/
│   ├── AGENT.md        # AI agent 快速参考
│   └── QQ_BOT_TROUBLESHOOTING.md  # QQ 双模式调试指南
└── CLAUDE.md           # Claude Code 工作指引
```

## 技术栈

- **AI 后端** — Claude API (Anthropic SDK)
- **QQ 框架** — NoneBot2 + OneBot v11 适配器 / QQ 官方适配器
- **QQ 协议** — NapCatQQ（可选）
- **数据存储** — SQLite
- **联网工具** — DuckDuckGo Search + httpx + BeautifulSoup
