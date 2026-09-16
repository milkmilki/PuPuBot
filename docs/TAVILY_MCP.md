# Tavily MCP 接入

PuPu 支持把 Tavily 作为外部 MCP server 接入：

https://github.com/tavily-ai/tavily-mcp

这个 server 提供：

- `tavily_search`：网页搜索
- `tavily_extract`：抓取网页正文
- `tavily_crawl`：站点爬取
- `tavily_map`：站点地图
- `tavily_research`：综合研究

它需要 `TAVILY_API_KEY`，并且本机需要能运行 Node/npm。

## 配置

Windows 推荐在 `pupu.yaml` 里配置：

```yaml
mcp:
  servers:
    tavily:
      enabled: true
      command: cmd
      args: ["/c", "npx", "-y", "tavily-mcp@latest"]
      exposures: ["chat", "proactive"]
      timeout: 30
      env:
        TAVILY_API_KEY: "你的 Tavily API key"
        DEFAULT_PARAMETERS: '{"search_depth":"basic","max_results":5}'
```

macOS/Linux 推荐：

```yaml
mcp:
  servers:
    tavily:
      enabled: true
      command: npx
      args: ["-y", "tavily-mcp@latest"]
      exposures: ["chat", "proactive"]
      timeout: 30
      env:
        TAVILY_API_KEY: "你的 Tavily API key"
        DEFAULT_PARAMETERS: '{"search_depth":"basic","max_results":5}'
```

修改 `pupu.yaml` 后需要重启 PuPu 实例或刷新工具 registry。

## 使用范围

外部 MCP server 由 PuPu 内置 stdio MCP client 接入，然后统一注册进工具表。DeepSeek、Anthropic 等 LLM API provider 会看到普通 tool schema，真正执行时仍由 PuPu 复用对应的 MCP stdio session。

成功后，工具名会类似：

```text
mcp__tavily__tavily_search
mcp__tavily__tavily_extract
mcp__tavily__tavily_crawl
mcp__tavily__tavily_map
mcp__tavily__tavily_research
```

## 运行方式和内存

PuPu 的内置 MCP client 会为每个启用的外部 stdio MCP server 保持一个常驻子进程：

- 启动时执行 `initialize` 和 `tools/list`。
- 工具调用时复用同一个连接执行 `tools/call`。
- 如果子进程异常退出，下一次工具调用会自动重启并重新初始化。
- PuPu 退出或刷新工具 registry 时会关闭旧 session。

这样比每次调用都重新执行 `npx` 快很多，也更接近常规 MCP runtime。代价是每个外部 MCP server 会常驻占用一份进程内存。`tavily-mcp` 是 Node 进程，通常是几十到一百多 MB 量级，具体取决于 Node/npm 和 server 自身依赖。

## 验证

先确认 Node/npm 可用：

```powershell
where.exe node
where.exe npx
```

如果 `where.exe npx` 找不到，需要先安装 Node.js LTS，或把 `pupu.yaml` 的 `command` 改成真实 `npx.cmd` 路径。

然后在项目根目录运行：

```powershell
.\ForFun\Scripts\python.exe -c "from pupu.app_config import apply_app_config_env; apply_app_config_env(override=True); from pupu.tools import describe_tool_servers, get_chat_tool_definitions; print(describe_tool_servers()); print([t['name'] for t in get_chat_tool_definitions() if 'tavily' in t['name']])"
```

期望看到 `tavily` server，以及 `mcp__tavily__tavily_search` 等工具名。

如果看到：

```text
[pupu][mcp] skip external server 'tavily': ... 'npx' is not recognized ...
```

说明 PuPu 已读到配置，但当前环境没有可执行的 `npx`。
