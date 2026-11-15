@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   MarsadAgent setup
echo ============================================

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo [!] Please right-click setup_agent.bat and choose "Run as administrator"
  pause
  exit /b 1
)

set PY=
for /f "delims=" %%i in ('where python 2^>nul ^| find /i /v "WindowsApps"') do if not defined PY set PY=%%i
if not defined PY if exist "%ProgramFiles%\Python313\python.exe" set PY=%ProgramFiles%\Python313\python.exe
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set PY=%ProgramFiles%\Python312\python.exe
if not defined PY (
  echo [!] Python is not installed ^(the WindowsApps shortcut is not real Python^).
  echo     Install Python 3.11 or newer from https://www.python.org/downloads/windows/
  echo     IMPORTANT: tick "Add python.exe to PATH" and choose "Install for all users".
  echo     Or run:  winget install -e --id Python.Python.3.12 --scope machine
  pause
  exit /b 1
)
for %%i in ("%PY%") do set PYDIR=%%~dpi
set PYW=%PYDIR%pythonw.exe
echo Python: %PY%

echo.
echo [1/3] Installing database drivers...
for %%p in (pyodbc pymysql pg8000 fdb) do (
  "%PY%" -m pip install --upgrade --quiet %%p || echo [!] could not install %%p - only needed if your database uses it
)

echo.
echo [2/3] Testing connections (database + server)...
"%PY%" agent.py --test
echo.
echo If the database test failed: edit config.json then run this file again.
echo.

echo [3/3] Registering automatic start (every 5 minutes, also after restart)...
schtasks /Delete /TN "MarsadAgent" /F >nul 2>&1
schtasks /Create /TN "MarsadAgent" /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /F /TR "\"%PYW%\" \"%~dp0agent.py\" --run-for 290"
if %errorlevel% neq 0 (
  echo [!] Could not create the scheduled task.
) else (
  echo Done. Starting the agent now...
  schtasks /Run /TN "MarsadAgent" >nul
)
echo.
echo Logs: %~dp0agent.log
pause
