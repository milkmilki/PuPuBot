@echo off
chcp 65001 >nul 2>&1
REM 用 cmd /k 保留窗口，服务退出或 Ctrl+C 后仍可见输出，方便复制报错。
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  cmd /k ".venv\Scripts\python.exe -m pupu_console.arbiter_server %*"
) else (
  cmd /k "python -m pupu_console.arbiter_server %*"
)
