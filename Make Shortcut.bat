@echo off
REM Creates a working "Run StudyBridge.lnk" in THIS folder (native Windows shortcut).
REM Run this once; then double-click the shortcut to start the app.
cd /d "%~dp0"
set "SBFOLDER=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f=$env:SBFOLDER.TrimEnd('\'); $ws=New-Object -ComObject WScript.Shell; $sc=$ws.CreateShortcut((Join-Path $f 'Run StudyBridge.lnk')); $sc.TargetPath=(Join-Path $PSHOME 'powershell.exe'); $sc.Arguments=('-NoProfile -ExecutionPolicy Bypass -File \"' + (Join-Path $f 'run.ps1') + '\"'); $sc.WorkingDirectory=$f; $sc.IconLocation=(Join-Path $PSHOME 'powershell.exe'); $sc.Description='Start StudyBridge'; $sc.Save(); Write-Host ('Created: ' + (Join-Path $f 'Run StudyBridge.lnk'))"
echo.
pause
