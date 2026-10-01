@echo off
setlocal

set "CVS_ROOT=%~dp0.."
if not defined INDEX_TTS_ROOT set "INDEX_TTS_ROOT=%~dp0..\..\index-tts"

for %%I in ("%INDEX_TTS_ROOT%") do set "INDEX_TTS_ROOT=%%~fI"
for %%I in ("%CVS_ROOT%") do set "CVS_ROOT=%%~fI"

if not defined INDEX_TTS_PYTHON set "INDEX_TTS_PYTHON=%INDEX_TTS_ROOT%\.venv\Scripts\python.exe"

if not exist "%INDEX_TTS_PYTHON%" (
  echo ERROR: IndexTTS Python environment not found:
  echo   %INDEX_TTS_PYTHON%
  exit /b 2
)

if not defined INDEX_TTS_MODEL_DIR set "INDEX_TTS_MODEL_DIR=%INDEX_TTS_ROOT%\checkpoints"
if not defined INDEX_TTS_HOST set "INDEX_TTS_HOST=127.0.0.1"
if not defined INDEX_TTS_PORT set "INDEX_TTS_PORT=9882"
if not defined INDEX_TTS_USE_BF16 set "INDEX_TTS_USE_BF16=1"
if not defined INDEX_TTS_USE_QWEN_EMO set "INDEX_TTS_USE_QWEN_EMO=0"

cd /d "%INDEX_TTS_ROOT%"
"%INDEX_TTS_PYTHON%" "%CVS_ROOT%\sidecars\index_tts_api.py"
exit /b %ERRORLEVEL%
