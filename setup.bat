@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
set "SETUP_EXITCODE=%ERRORLEVEL%"
echo.
echo %cmdcmdline% | find /i "%~f0" >nul && pause
exit /b %SETUP_EXITCODE%
