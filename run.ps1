# StudyBridge launcher (run.ps1)
# - Stops any old server still using the port
# - Installs Python packages on the first run only
# - Starts the server and opens the browser
# Keep this file ASCII-only.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "==============================================="
Write-Host " StudyBridge starting..."
Write-Host " Folder: $here"
Write-Host "==============================================="

# --- 0. Create a working double-click shortcut in this folder (once) ---
# A real Windows shortcut must be made on Windows, so we build it here the first time.
$lnk = Join-Path $here "Run StudyBridge.lnk"
if (-not (Test-Path $lnk)) {
    try {
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = (Join-Path $PSHOME "powershell.exe")
        $sc.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $here "run.ps1") + '"'
        $sc.WorkingDirectory = $here
        $sc.IconLocation = (Join-Path $PSHOME "powershell.exe")
        $sc.Description = "Start StudyBridge"
        $sc.Save()
        Write-Host "Created 'Run StudyBridge.lnk' - double-click that next time."
    } catch {
        Write-Host "(Could not create shortcut automatically; you can run 'Make Shortcut.bat'.)"
    }
}

# --- 1. Find Python ---
$py = "python"
try { & $py --version *> $null } catch { $py = "py" }
try { & $py --version *> $null } catch {
    Write-Host "ERROR: Python was not found. Install Python 3 from python.org and try again."
    Read-Host "Press Enter to close"
    exit 1
}
Write-Host "Using Python command: $py"

# --- 2. Stop any old server on port 8080 ---
Write-Host "Stopping any old server on port 8080..."
try {
    $conns = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        Write-Host ("  killing leftover process PID " + $c.OwningProcess)
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
} catch { }
Start-Sleep -Milliseconds 500

# --- 3. Install packages on first run only ---
$marker = Join-Path $here ".deps_installed"
if (-not (Test-Path $marker)) {
    Write-Host "First run: installing Python packages (this happens only once)..."
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $here "requirements.txt")
    if ($LASTEXITCODE -eq 0) {
        New-Item -ItemType File -Path $marker -Force | Out-Null
        Write-Host "Packages installed."
    } else {
        Write-Host "ERROR: package install failed. Check your internet connection."
        Read-Host "Press Enter to close"
        exit 1
    }
}

# --- 4. Check the API key ---
$keyFile = Join-Path $here "OpenAI_Key.txt"
if (-not (Test-Path $keyFile) -or ((Get-Content $keyFile -Raw).Trim().Length -lt 10)) {
    Write-Host ""
    Write-Host "NOTE: OpenAI_Key.txt is empty. Open it and paste your OpenAI key,"
    Write-Host "      then run again. (The screens will load, but AI features need the key.)"
    Write-Host ""
}

# --- 5. Start the server in its own window ---
Write-Host "Starting server at http://127.0.0.1:8080 ..."
Start-Process -FilePath $py -ArgumentList @("-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8080") -WorkingDirectory $here

# --- 6. Wait, then open the browser ---
Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:8080"
Write-Host "Done. The browser should open. (Server runs in its own window.)"
Start-Sleep -Seconds 2
