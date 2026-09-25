@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
set "VENV_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo Project environment missing. Run first_setup.cmd first.
  pause
  exit /b 1
)
pushd "%PROJECT_ROOT%"
"%VENV_PYTHON%" -c "from server.config import ADMIN_TOKEN; print('Library token:', ADMIN_TOKEN)"
popd
pause
