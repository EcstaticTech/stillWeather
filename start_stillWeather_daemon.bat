@echo off
setlocal
title stillWeather GOES-19 GLM ^& IEM NEXRAD Ingest Daemon

REM Change working directory to repository root
cd /d "%~dp0"

REM Use local virtualenv Python if available, otherwise system Python
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo ============================================================================
echo Starting stillWeather Ingest Daemon
echo Working Directory : %CD%
echo Python Executable : %PYTHON_EXE%
echo ============================================================================

:loop
"%PYTHON_EXE%" -m ingest.daemon %*
set EXIT_CODE=%ERRORLEVEL%
echo [%DATE% %TIME%] Daemon exited with code %EXIT_CODE%. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
