@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   仆仆 Web 控制台启动器
echo ========================================
echo.

if not exist "ForFun\Scripts\python.exe" (
    echo [ERROR] 未找到虚拟环境: ForFun\Scripts\python.exe
    echo 请先双击 deploy.bat 完成初始化。
    pause
    exit /b 1
)

echo [1/2] 检查 Web UI 依赖...
ForFun\Scripts\python.exe -c "import fastapi, uvicorn, multipart" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 正在安装依赖 requirements.txt ...
    ForFun\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败，请检查网络或 pip 源。
        pause
        exit /b 1
    )
)

echo [2/2] 启动控制台...
echo 默认地址: http://127.0.0.1:8770/
echo.
ForFun\Scripts\python.exe -m pupu_console

pause
