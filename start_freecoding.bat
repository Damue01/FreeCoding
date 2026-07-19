@echo off
setlocal EnableExtensions

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"
title FreeCoding
if not defined FREECODING_PORT set "FREECODING_PORT=8000"
set "FREECODING_URL=http://127.0.0.1:%FREECODING_PORT%"

echo [FreeCoding] Project directory: %CD%

powershell.exe -NoProfile -Command "try { $health = Invoke-RestMethod '%FREECODING_URL%/health' -TimeoutSec 2; if ($health.ok) { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo [FreeCoding] The service is already running at %FREECODING_URL%/
    if /I not "%FREECODING_NO_BROWSER%"=="1" start "" "%FREECODING_URL%/"
    exit /b 0
)

set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [FreeCoding] First run: creating a Python 3.11 virtual environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3.11 -m venv "%PROJECT_ROOT%.venv"
    ) else (
        python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python 3.11 was not found. Install it and run this file again.
            pause
            exit /b 1
        )
        python -m venv "%PROJECT_ROOT%.venv"
    )
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "from importlib.metadata import version; [version(name) for name in ('fastapi', 'uvicorn', 'pydantic-settings', 'paddleocr', 'paddlepaddle', 'opencv-contrib-python')]" >nul 2>&1
if errorlevel 1 (
    echo [FreeCoding] Installing runtime dependencies. The first install may take a while...
    "%PYTHON_EXE%" -m pip install --upgrade pip
    if errorlevel 1 goto :install_failed
    "%PYTHON_EXE%" -m pip install -e ".[ocr,client]"
    if errorlevel 1 goto :install_failed
)

if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo [FreeCoding] Created .env with the mock driver.
    echo [FreeCoding] Configure vivo_adb, the ADB path, and the device serial for a real phone.
)

echo [FreeCoding] Starting %FREECODING_URL%/
echo [FreeCoding] Press Ctrl+C in this window to stop the service.
if /I not "%FREECODING_NO_BROWSER%"=="1" start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%FREECODING_URL%/'"

"%PYTHON_EXE%" -m app2api.cli serve --host 127.0.0.1 --port %FREECODING_PORT%
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] FreeCoding exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

:install_failed
echo.
echo [ERROR] Dependency installation failed. Check Python, network, and disk space.
pause
exit /b 1
