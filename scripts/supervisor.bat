@echo off
setlocal
:loop
call "%~dp0..\start.bat"
if errorlevel 2 (exit /b 1)
echo TapeWebUI exited, restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
