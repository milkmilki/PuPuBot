# Codex CLI 使用 Clash 专用代理

这份文档记录 PuPu 如何在不打开 Windows 系统代理的情况下，让 PuPu 启动的 Codex CLI 单独走 Clash。

## 结论

- Clash 的 `7890` 是本机入站监听端口，通常是 `mixed-port`，不是某个远端节点。
- PuPu 通过 `PUPU_CODEX_PROXY=http://127.0.0.1:7890` 给 Codex CLI 子进程注入代理环境变量。
- Windows 系统代理可以关闭，但 Clash 程序和 clash core 必须运行，并且 `127.0.0.1:7890` 必须处于监听状态。
- Codex 最终走哪个节点由 Clash 当前模式、规则和代理组选择决定，PuPu 不直接指定节点。

## 配置

在项目根目录 `.env` 中配置：

```env
# Optional proxy used only by PuPu-launched Codex CLI subprocesses.
# Keep Clash running, but system proxy can be off.
PUPU_CODEX_PROXY=http://127.0.0.1:7890
PUPU_CODEX_NO_PROXY=localhost,127.0.0.1,::1
```

修改 `.env` 后需要重启 PuPu，否则运行中的进程不会重新加载环境变量。

## 代码路径

相关实现位于：

- `pupu/llm_providers.py`
- `_codex_subprocess_env()`：读取 `PUPU_CODEX_PROXY`，生成 Codex 子进程环境变量。
- `CodexCliProvider.check_available()`：执行 `codex --version` 和 `codex login status` 时也使用代理环境。
- `CodexCliProvider._run_codex_exec()`：执行 `codex exec` 时使用代理环境。
- `_default_codex_command()`：优先使用可直接执行的 Codex CLI，比如 npm 或 VS Code 扩展里的 `codex.exe`，再回退到 PATH。

注入给 Codex 子进程的变量包括：

```text
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
ALL_PROXY=http://127.0.0.1:7890
NO_PROXY=localhost,127.0.0.1,::1
```

## 验证命令

确认 Clash core 正在监听 `7890`：

```powershell
Get-NetTCPConnection -LocalPort 7890 -ErrorAction SilentlyContinue |
  Select-Object LocalAddress,LocalPort,State,OwningProcess
```

期望看到类似：

```text
127.0.0.1  7890  Listen
```

确认 PuPu 会给 Codex 子进程注入代理：

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv('.env'); import os; from pupu.llm_providers import _default_codex_command, _codex_subprocess_env; env=_codex_subprocess_env(); print('codex=', _default_codex_command()); print('PUPU_CODEX_PROXY=', os.environ.get('PUPU_CODEX_PROXY')); print('HTTPS_PROXY in child=', None if env is None else env.get('HTTPS_PROXY')); print('NO_PROXY in child=', None if env is None else env.get('NO_PROXY'))"
```

确认 Codex CLI 可用：

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv('.env'); from pupu.llm import codex_cli_status; print(codex_cli_status())"
```

期望输出：

```text
ok
```

运行相关测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_llm_providers
```

## 查看实际走哪个节点

PuPu 不选择 Clash 节点。实际节点由 Clash 决定。

如果 Clash 开了 external controller，可以查询当前模式和代理组。端口以 Clash 配置里的 `external-controller` 为准，例如 Clash for Windows 可能是 `127.0.0.1:1772`：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:1772/configs'
```

查看代理组当前选择：

```powershell
$p = Invoke-RestMethod -Uri 'http://127.0.0.1:1772/proxies'
$p.proxies.PSObject.Properties |
  Where-Object { $_.Name -in @('🔰 选择节点','🐟 漏网之鱼','GLOBAL') } |
  ForEach-Object { $_.Name + ' => now=' + $_.Value.now + ', type=' + $_.Value.type }
```

查看当前连接链路：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:1772/connections' |
  ConvertTo-Json -Depth 6
```

常见结果：

```text
chatgpt.com / chat.openai.com
rule = DomainSuffix(chatgpt.com) 或 DomainKeyword(openai)
chains = [具体节点, 代理组]
```

如果 `🔰 选择节点` 当前选中 `香港A01`，Codex/ChatGPT 流量就会走 `香港A01`。切换该代理组后，PuPu 的 Codex 流量也会随之切换。

## 常见问题

### 系统代理关了还能用吗

可以。PuPu 是通过子进程环境变量让 Codex CLI 走代理，不依赖 Windows 系统代理。

但 Clash core 必须开着。否则 `127.0.0.1:7890` 没有服务监听，Codex 连接会失败。

### 7890 是发出端口吗

不是。对本机程序来说，`7890` 是 Clash 的本地入站监听端口。PuPu/Codex 连接 `127.0.0.1:7890`，Clash 再按规则选择远端节点。

### 为什么配置了代理还是失败

先查三件事：

1. `.env` 里是否有 `PUPU_CODEX_PROXY=http://127.0.0.1:7890`。
2. PuPu 是否在修改 `.env` 后重启过。
3. `Get-NetTCPConnection -LocalPort 7890` 是否能看到 `Listen`。

### `codex_cli_status()` 报 `WinError 5 拒绝访问`

通常是自动探测到了 WindowsApps 里的 Codex 入口，Python 子进程无法直接启动。

解决方式：

- 使用当前代码里的探测顺序，优先找 npm 或 VS Code 扩展中的真实 `codex.exe`。
- 或在 `.env` 显式指定：

```env
PUPU_CODEX_BIN=C:\Users\<用户名>\.vscode\extensions\<openai.chatgpt-版本>\bin\windows-x86_64\codex.exe
```

### 怎么确认不是走 DIRECT

看 Clash 日志或 external controller 的 `/connections`。如果 ChatGPT/OpenAI 连接的 `chains` 包含具体节点和代理组，就不是 DIRECT。

例如：

```text
chains = [香港A01, 🔰 选择节点]
```

如果显示：

```text
chains = [DIRECT]
```

说明 Clash 规则把该域名直连了，需要调整 Clash 规则或代理组选择。
