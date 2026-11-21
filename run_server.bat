@echo off
chcp 65001 > nul
setlocal

cd /d "%~dp0"
echo ========================================================
echo   Readout - Marathon Audio PWA Server
echo   Running at http://127.0.0.1:8002
echo ========================================================

if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8002 --reload
) else (
    echo [ERROR] Virtual environment not found at venv\Scripts\python.exe
    pause
)
