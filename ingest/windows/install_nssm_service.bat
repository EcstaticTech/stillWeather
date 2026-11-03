@echo off
setlocal
REM ============================================================================
REM Register stillWeatherDaemon as a persistent Windows Service via NSSM
REM Run from an elevated (Administrator) Command Prompt or PowerShell
REM ============================================================================

set "SERVICE_NAME=stillWeatherDaemon"
for %%I in ("%~dp0..\..") do set "REPO_ROOT=%%~fI"
set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
set "LOG_DIR=%REPO_ROOT%\logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo Installing %SERVICE_NAME% from %REPO_ROOT% ...
nssm install %SERVICE_NAME% "%PYTHON_EXE%" "-m ingest.daemon"
nssm set %SERVICE_NAME% AppDirectory "%REPO_ROOT%"
nssm set %SERVICE_NAME% Description "stillWeather GOES-19 GLM & IEM NEXRAD Lightning Ingest Daemon"
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START
nssm set %SERVICE_NAME% AppStdout "%LOG_DIR%\daemon.log"
nssm set %SERVICE_NAME% AppStderr "%LOG_DIR%\daemon.log"
nssm set %SERVICE_NAME% AppRotateFiles 1
nssm set %SERVICE_NAME% AppRotateOnline 1
nssm set %SERVICE_NAME% AppRotateBytes 5242880
nssm set %SERVICE_NAME% AppThrottle 5000
nssm set %SERVICE_NAME% AppExit Default Restart
nssm set %SERVICE_NAME% AppRestartDelay 10000

echo Starting %SERVICE_NAME% ...
nssm start %SERVICE_NAME%
nssm status %SERVICE_NAME%
