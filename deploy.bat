@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   Deploying pupu...
echo ========================================
echo.

:: Pick Python. Python 3.12/3.13 is the normal path because optional memU
:: dependencies may not support Python 3.14 yet.
set "PY_CMD="
py -3.13 --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3.13"
if "%PY_CMD%"=="" (
    py -3.12 --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3.12"
)
if "%PY_CMD%"=="" (
    py -3.14 --version >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=py -3.14"
        echo [WARN] Python 3.14 detected. Base PuPuBot can run, but optional memU may require manual install.
    )
)
if "%PY_CMD%"=="" (
    echo [ERROR] Python 3.12 or 3.13 not found. Please install Python 3.13 first.
    pause
    exit /b 1
)

:: Create venv if missing
if not exist "ForFun\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    %PY_CMD% -m venv ForFun
) else (
    echo [1/3] Virtual environment already exists.
)

:: Install deps
echo [2/3] Installing dependencies...
ForFun\Scripts\pip install -r requirements.txt -q
echo Optional memU semantic cache:
echo   ForFun\Scripts\pip install -r requirements-memu.txt

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
