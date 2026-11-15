@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Quick connection test only (database + server). Nothing is installed.
py agent.py --test 2>nul || python agent.py --test
pause
