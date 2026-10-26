# One-command local launch (no Docker). Run from the Prototype folder:  ./start-local.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$be = Join-Path $root "backend"
$fe = Join-Path $root "frontend"

if (-not (Test-Path "$be\.venv")) {
  Write-Host "Creating Python venv ..."
  python -m venv "$be\.venv"
  & "$be\.venv\Scripts\python.exe" -m pip install -q -r "$be\requirements.txt"
}
if (-not (Test-Path "$fe\node_modules")) {
  Write-Host "Installing frontend packages ..."
  Push-Location $fe; npm install --no-audit --no-fund; Pop-Location
}
Write-Host "Seeding synthetic demo data ..."
Push-Location $be; & ".venv\Scripts\python.exe" -W ignore -m app.seed; Pop-Location

Write-Host "Starting API on :8000 and web on :5173 (close these windows to stop) ..."
Start-Process powershell -WorkingDirectory $be -ArgumentList "-NoExit", "-Command", ".\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"
Start-Process powershell -WorkingDirectory $fe -ArgumentList "-NoExit", "-Command", "npm run dev"
Start-Sleep -Seconds 4
Start-Process "http://localhost:5173"
Write-Host "Open http://localhost:5173  (demo password for all accounts: Demo@1234)"
