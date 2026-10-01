@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0first_setup.ps1"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
