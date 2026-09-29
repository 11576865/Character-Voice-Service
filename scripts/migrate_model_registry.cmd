@echo off
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\migrate_model_registry.py" %*
) else (
  python "scripts\migrate_model_registry.py" %*
)
if errorlevel 1 pause
endlocal
