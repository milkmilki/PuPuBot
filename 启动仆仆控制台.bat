@echo off
cd /d "%~dp0"

set "PY=ForFun\Scripts\python.exe"

echo ========================================
echo   PuPu Web Console Launcher
echo ========================================
echo.

if not exist "%PY%" (
    echo [ERROR] Virtual environment not found: %PY%
    echo Run deploy.bat first.
    pause
    exit /b 1
)

if not exist "pupu.yaml" (
    echo [INFO] pupu.yaml not found.
    echo The console will create it from pupu.yaml.example on first run.
    echo Fill llm.*.api_key in pupu.yaml before starting an instance.
    echo.
)

echo [1/2] Checking Web UI dependencies...
"%PY%" -c "import fastapi, uvicorn, multipart" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies from requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
)

echo [2/2] Starting console...
echo Default URL: http://127.0.0.1:8770/
echo.
start "PuPu Console" cmd /k ""%PY%" -m pupu_console"
exit /b
