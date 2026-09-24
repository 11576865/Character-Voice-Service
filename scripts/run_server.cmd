@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
set "VENV_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
  echo ERROR: Project virtual environment was not found. Run .\scripts\first_setup.cmd first.
  exit /b 1
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
