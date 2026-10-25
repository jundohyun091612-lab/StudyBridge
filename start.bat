@echo off
REM Double-click this file to start StudyBridge.
cd /d "%~dp0"
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
echo.
echo If the browser did not open, go to http://127.0.0.1:8080 yourself.
pause
