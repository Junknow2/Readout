@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\Jun\Documents\Dev\Readout"
if not exist "data\logs" mkdir "data\logs"
"C:\Users\Jun\Documents\Dev\Readout\venv\Scripts\python.exe" curator_engine.py --run >> "data\logs\curator_daily.log" 2>&1
endlocal
