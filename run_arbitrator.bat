@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

REM 必须在本仓库根目录执行，否则 -m pupu_console 会找不到包。
echo.
echo [arbiter] cwd=%cd%
echo [arbiter] 手工调试可复制下面两行到 cmd（把报错完整发出来）:
echo   cd /d "%cd%"
echo   .venv\Scripts\python.exe -m pupu_console.arbiter_server
echo.
echo 若报 No module named uvicorn / fastapi：请先安装依赖（用仓库 venv）:
echo   .venv\Scripts\pip.exe install -r requirements.txt
echo.

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
echo [arbiter] using "%PY%"
"%PY%" -m pupu_console.arbiter_server %*
if errorlevel 1 (
  echo.
  echo [arbiter] 启动失败，退出码非 0。请把上方报错全文复制发给我。
  pause
)
