@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
set "VENV_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "RUNTIME_REGISTRY=%PROJECT_ROOT%\config\runtimes.local.json"

if not exist "%VENV_PYTHON%" (
  echo ERROR: Project virtual environment was not found. Run .\scripts\first_setup.cmd first.
  exit /b 1
)

if not exist "%RUNTIME_REGISTRY%" (
  echo Runtime Registry not found. Creating machine-local configuration...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\scripts\init_runtime_registry.ps1"
  if errorlevel 1 exit /b %ERRORLEVEL%
)

pushd "%PROJECT_ROOT%"
"%VENV_PYTHON%" -m server.voice_profiles
if errorlevel 1 (
  set "SERVER_EXIT=%ERRORLEVEL%"
  popd
  exit /b %SERVER_EXIT%
)
"%VENV_PYTHON%" -m server.app
set "SERVER_EXIT=%ERRORLEVEL%"
popd
exit /b %SERVER_EXIT%
