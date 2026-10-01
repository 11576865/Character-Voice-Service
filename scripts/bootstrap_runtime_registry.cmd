@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap_runtime_registry.ps1" %*
exit /b %ERRORLEVEL%
