@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
set "VENV_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
  echo Project environment missing. Run first_setup.cmd first.
  pause
  exit /b 1
)

set "SOURCE_VOICES=%~1"
set "MODEL_ROOT=%~2"
if "%SOURCE_VOICES%"=="" set /p "SOURCE_VOICES=Folder containing character folders: "
if "%MODEL_ROOT%"=="" set /p "MODEL_ROOT=GPT-SoVITS root folder with GPT_weights_v4 and SoVITS_weights_v4: "
pushd "%PROJECT_ROOT%"
echo Import preview:
"%VENV_PYTHON%" -m server.folder_import "%SOURCE_VOICES%" "%MODEL_ROOT%"
if errorlevel 1 (
  popd
  pause
  exit /b 1
)
choice /C YN /M "Copy these WAV files and update the character profiles"
if errorlevel 2 (
  popd
  exit /b 0
)
"%VENV_PYTHON%" -m server.folder_import "%SOURCE_VOICES%" "%MODEL_ROOT%" --apply
set "IMPORT_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %IMPORT_EXIT%
