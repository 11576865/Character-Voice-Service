@echo off
setlocal
set "PROJECT_ROOT=%~dp0.."
set "VENV_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo Project environment missing. Run first_setup.cmd first.
  pause
  exit /b 1
)
set "PACK=%~1"
set "VOICE=%~2"
if "%PACK%"=="" set /p "PACK=Reference Pack folder: "
if "%VOICE%"=="" set /p "VOICE=Existing character ID: "
pushd "%PROJECT_ROOT%"
"%VENV_PYTHON%" -m server.reference_import "%PACK%" "%VOICE%"
set "IMPORT_EXIT=%ERRORLEVEL%"
popd
if not "%~1"=="" exit /b %IMPORT_EXIT%
pause
exit /b %IMPORT_EXIT%
