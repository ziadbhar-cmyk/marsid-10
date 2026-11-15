@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Run the agent in this window for 3 minutes to watch what it does (for troubleshooting)
python agent.py --run-for 180 -v
pause
