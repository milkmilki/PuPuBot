@echo off
cd /d "%~dp0"

set "PY=ForFun\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Virtual environment not found: %PY%
    echo Run deploy.bat first.
    pause
    exit /b 1
)

if not exist "pupu.yaml" (
    echo [INFO] pupu.yaml not found.
    echo The launcher will create it from pupu.yaml.example on first run.
    echo Fill llm.*.api_key in pupu.yaml before chatting or starting QQ.
    echo.
)

start "PuPu" cmd /k ""%PY%" start.py"
exit /b
