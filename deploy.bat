@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   Deploying pupu...
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ first.
    pause
    exit /b 1
)

:: Create venv if missing
if not exist "ForFun\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv ForFun
) else (
    echo [1/3] Virtual environment already exists.
)

:: Install deps
echo [2/3] Installing dependencies...
ForFun\Scripts\pip install -r requirements.txt -q

:: Check .env
if not exist ".env" (
    echo.
    echo [WARNING] .env file not found!
    echo Please create .env with your API key:
    echo   ANTHROPIC_BASE_URL=your_base_url
    echo   ANTHROPIC_API_KEY=your_api_key
    echo.
)

:: Done
echo [3/3] Done!
echo.
echo To start pupu, run:
echo   ForFun\Scripts\python.exe start.py
echo   or double-click: deploy.bat
echo.

:: Ask to start
set /p START="Start pupu now? (y/N): "
if /i "%START%"=="y" (
    ForFun\Scripts\python.exe start.py
)

pause
