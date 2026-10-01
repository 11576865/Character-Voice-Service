@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap_runtime_registry.ps1" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
