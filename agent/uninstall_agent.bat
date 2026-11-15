@echo off
schtasks /Delete /TN "MarsadAgent" /F
echo MarsadAgent scheduled task removed.
pause
