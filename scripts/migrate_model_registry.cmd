@echo off
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m scripts.migrate_model_registry %*
) else (
  python -m scripts.migrate_model_registry %*
)
if errorlevel 1 pause
endlocal
