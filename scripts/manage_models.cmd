@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo CVS virtual environment is missing: %CD%\.venv
  exit /b 1
)
".venv\Scripts\python.exe" -m server.model_cli %*
exit /b %ERRORLEVEL%
