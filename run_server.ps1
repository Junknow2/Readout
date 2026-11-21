$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Readout - Marathon Audio PWA Server" -ForegroundColor Green
Write-Host "  Running at http://127.0.0.1:8002" -ForegroundColor Yellow
Write-Host "========================================================" -ForegroundColor Cyan

$PythonPath = Join-Path $ScriptDir "venv\Scripts\python.exe"

if (Test-Path $PythonPath) {
    & $PythonPath -m uvicorn main:app --host 127.0.0.1 --port 8002 --reload
} else {
    Write-Error "Virtual environment not found at $PythonPath"
}
