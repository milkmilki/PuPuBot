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

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8770/api/instances' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Console is already running.
    start "" "http://127.0.0.1:8770/"
    exit /b 0
)

if not exist "logs\launcher" mkdir "logs\launcher"
start "PuPu Console" cmd /k ""%PY%" -m pupu_console 1>>"logs\launcher\console.log" 2>>&1"

echo [INFO] Waiting for console to become ready...
for /l %%i in (1,1,30) do (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8770/api/instances' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 (
        echo [OK] Console is ready.
        start "" "http://127.0.0.1:8770/"
        exit /b 0
    )
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" >nul 2>&1
)

echo [ERROR] Console did not become ready in time.
echo See logs\launcher\console.log for details.
pause
exit /b 1
