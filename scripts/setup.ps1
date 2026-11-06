# One-time TapeWebUI installer: venv, dependencies, frontend build,
# optional logon scheduled task (elevated: tape access needs admin).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "=== TapeWebUI setup ===" -ForegroundColor Cyan

# --- Python check (py launcher, 3.11+) ---
$python = $null
foreach ($candidate in @('py -V:3.12', 'py -V:3.11', 'py -V:3.13')) {
  $name, $ver = $candidate.Split(' ')
  & $name $ver -c "1" 2>$null
  if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) {
  Write-Error "Python 3.11+ not found via py launcher. Install Python first."
}
Write-Host "Using: $python"

# --- venv ---
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
  Write-Host "Creating .venv..."
  $parts = $python.Split(' ')
  & $parts[0] $parts[1] -m venv "$root\.venv"
  if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed" }
}

Write-Host "Installing Python dependencies..."
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
& "$root\.venv\Scripts\python.exe" -m pip install -e "$root[dev]" -q
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed" }

# --- Node build ---
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Write-Warning "npm not found: the frontend will not be rebuilt (static/dist is shipped prebuilt)."
} else {
  Write-Host "Building frontend..."
  Push-Location "$root\frontend"
  npm install --no-audit --no-fund
  if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed" }
  npm run build
  if ($LASTEXITCODE -ne 0) { Write-Error "npm build failed" }
  Pop-Location
}

# --- ITDT check ---
$itdt = 'C:\Program Files\ITDT\itdt.exe'
if (Test-Path $itdt) {
  Write-Host "ITDT found: $itdt" -ForegroundColor Green
} else {
  Write-Warning "ITDT not found at $itdt — set TAPE_ITDT_EXE in .env"
}

# --- Scheduled task (elevated logon task, restart-on-death supervisor) ---
$registerTask = $false
if (-not $env:TAPE_NO_TASK) {
  $answer = Read-Host "Register the 'TapeWebUI' logon task (elevated, auto-restart)? [y/N]"
  $registerTask = ($answer -eq 'y')
}
if ($registerTask) {
  $supervisor = Join-Path $root 'scripts\supervisor.bat'
  schtasks /create /f /tn "TapeWebUI" /sc onlogon /rl highest /tr "`"$supervisor`""
  if ($LASTEXITCODE -eq 0) {
    Write-Host "Task 'TapeWebUI' registered." -ForegroundColor Green
  } else {
    Write-Warning "Task registration failed (needs admin terminal)."
  }
}

Write-Host ""
Write-Host "Setup complete. Start with start.bat (port 8091)." -ForegroundColor Green
