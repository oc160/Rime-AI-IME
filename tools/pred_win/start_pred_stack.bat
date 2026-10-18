@echo off
rem AI-IME predict-v2 stack: sidecar (owns llama-server lifecycle on :8084)
rem Usage: start_pred_stack.bat [watch_dir]
setlocal
set "ROOT=%~dp0.."
set "WATCH=%APPDATA%\Rime"
if not "%~1"=="" set "WATCH=%~1"

rem already running? (match by command line)
wmic process where "commandline like '%%pred_sidecar%%'" get processid 2>nul | findstr /r "[0-9]" >nul && (
  echo [SKIP] pred_sidecar already running
  exit /b 0
)

echo [START] pred_sidecar  watch=%WATCH%
start "" /min "%SystemRoot%\py.exe" -3 "%ROOT%\scripts\pred_sidecar.py" --watch-dir "%WATCH%"
rem py.exe 不存在时退回 pythonw
if errorlevel 1 (
  start "" /min pythonw "%ROOT%\scripts\pred_sidecar.py" --watch-dir "%WATCH%"
)
exit /b 0
