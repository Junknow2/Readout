# Readout PWA バックグラウンド起動スクリプト (ポート8002)
$ErrorActionPreference = "SilentlyContinue"

$baseDir = "C:\Users\Jun\Documents\Dev\Readout"
$logDir = "$baseDir\data\logs"
if (!(Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$port8002Active = Get-NetTCPConnection -State Listen -LocalPort 8002 -ErrorAction SilentlyContinue
if (!$port8002Active) {
    $pythonExe = "$baseDir\venv\Scripts\python.exe"
    $startup = [wmiclass]"Win32_ProcessStartup"
    $startupConfig = $startup.CreateInstance()
    $startupConfig.ShowWindow = 0
    $process = [wmiclass]"Win32_Process"
    $cmd = "cmd.exe /c `"set PYTHONUTF8=1&& set PYTHONIOENCODING=utf-8&& `"$pythonExe`" -m uvicorn main:app --app-dir `"$baseDir`" --host 127.0.0.1 --port 8002 >> `"$logDir\uvicorn_stdout.log`" 2>> `"$logDir\uvicorn_stderr.log`"`""
    $process.Create($cmd, $baseDir, $startupConfig) | Out-Null
}
