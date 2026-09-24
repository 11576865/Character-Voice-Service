[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "ERROR: Project virtual environment was not found. Run .\scripts\first_setup.cmd first." -ForegroundColor Red
    exit 1
}

Push-Location $projectRoot
try {
    & $venvPython -m server.voice_profiles
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $venvPython -m server.app
    if ($LASTEXITCODE -ne 0) { throw "Character Voice Service exited with code $LASTEXITCODE." }
}
finally {
    Pop-Location
}
