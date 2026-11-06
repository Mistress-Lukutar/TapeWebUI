@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (exit /b 2)
if not exist static\dist\index.html (exit /b 2)
echo Starting TapeWebUI on http://127.0.0.1:8091
.venv\Scripts\python.exe run.py
