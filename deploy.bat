@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   Deploying pupu...
echo ========================================
echo.

:: Check Python 3.14
py -3.14 --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.14 not found. Please install Python 3.14+ first.
    pause
    exit /b 1
)

:: Create venv if missing
if not exist "ForFun\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    py -3.14 -m venv ForFun
) else (
    echo [1/3] Virtual environment already exists.
)

:: Install deps
echo [2/3] Installing dependencies...
ForFun\Scripts\pip install -r requirements.txt -q

:: Check pupu.yaml
if not exist "pupu.yaml" (
    echo.
    echo [INFO] pupu.yaml is not present yet.
    echo The launcher will create it from pupu.yaml.example on first run.
    echo Fill llm.*.api_key in pupu.yaml before chatting or starting QQ.
    echo.
)

:: Done
echo [3/3] Done!
echo.
echo To start pupu, run:
echo   ForFun\Scripts\python.exe start.py
echo   or double-click: 启动仆仆.bat
echo To manage instances in browser:
echo   double-click: 启动仆仆控制台.bat
echo The launcher will ask you to create or select an instance.
echo.

:: Ask to start
set /p START="Start pupu now? (y/N): "
if /i "%START%"=="y" (
    start "PuPu" cmd /k ""ForFun\Scripts\python.exe" start.py"
)

pause
