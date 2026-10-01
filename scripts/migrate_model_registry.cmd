@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project environment missing. Run scripts\first_setup.cmd first.
  exit /b 1
)

".venv\Scripts\python.exe" -m scripts.migrate_model_registry %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
endlocal & exit /b %RC%
