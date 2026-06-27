@echo off
cd /d "%~dp0"

set "PY=ForFun\Scripts\python.exe"
set "SIRI_DIR=%~dp0desktop\pupu-siri"
set "CONSOLE_URL=http://127.0.0.1:8770"

echo ========================================
echo   PuPu Siri One-click Launcher
echo ========================================
echo.

if not exist "%PY%" (
    echo [ERROR] Virtual environment not found: %PY%
    echo Run deploy.bat first.
    pause
    exit /b 1
)

if not exist "%SIRI_DIR%\package.json" (
    echo [ERROR] PuPu Siri project not found: %SIRI_DIR%
    echo Make sure you are on the siri branch.
    pause
    exit /b 1
)

if not exist "logs\launcher" mkdir "logs\launcher"

echo [1/4] Checking console dependencies...
"%PY%" -c "import fastapi, uvicorn, multipart" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing Python dependencies from requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Python dependency installation failed.
        pause
        exit /b 1
    )
)

echo [2/4] Starting PuPu Console if needed...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri '%CONSOLE_URL%/api/instances' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    start "PuPu Console" cmd /k ""%PY%" -m pupu_console 1>>"logs\launcher\console.log" 2>>&1"
    echo [INFO] Waiting for Console at %CONSOLE_URL% ...
    for /l %%i in (1,1,30) do (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri '%CONSOLE_URL%/api/instances' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
        if not errorlevel 1 goto console_ready
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" >nul 2>&1
    )
    echo [ERROR] Console did not become ready in time.
    echo See logs\launcher\console.log for details.
    pause
    exit /b 1
) else (
    echo [OK] Console is already running.
)

:console_ready
echo [OK] Console is ready.

echo [3/4] Selecting Node package manager...
set "INSTALL_CMD="
set "DEV_CMD="

where pnpm >nul 2>&1
if not errorlevel 1 (
    set "INSTALL_CMD=pnpm install"
    set "DEV_CMD=pnpm run dev"
    goto package_manager_ready
)

where corepack >nul 2>&1
if not errorlevel 1 (
    set "INSTALL_CMD=corepack pnpm install"
    set "DEV_CMD=corepack pnpm run dev"
    goto package_manager_ready
)

where npm.cmd >nul 2>&1
if not errorlevel 1 (
    set "INSTALL_CMD=npm.cmd install"
    set "DEV_CMD=npm.cmd run dev"
    goto package_manager_ready
)

echo [ERROR] No Node package manager found.
echo Install Node.js, then run this launcher again.
pause
exit /b 1

:package_manager_ready
echo [OK] Using: %DEV_CMD%

if not exist "%SIRI_DIR%\node_modules" (
    echo [INFO] Installing PuPu Siri desktop dependencies...
    pushd "%SIRI_DIR%"
    call %INSTALL_CMD%
    if errorlevel 1 (
        popd
        echo [ERROR] PuPu Siri dependency installation failed.
        pause
        exit /b 1
    )
    popd
)

echo [4/4] Starting PuPu Siri...
start "PuPu Siri" /D "%SIRI_DIR%" cmd /k "%DEV_CMD%"

echo.
echo [OK] PuPu Siri startup requested.
echo Console URL: %CONSOLE_URL%
echo Close the PuPu Siri window or its terminal to stop the desktop pet dev process.
exit /b 0
