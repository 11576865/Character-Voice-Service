[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runtimeRegistry = Join-Path $projectRoot "config\runtimes.local.json"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "ERROR: Project virtual environment was not found. Run .\scripts\first_setup.cmd first." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $runtimeRegistry -PathType Leaf)) {
    Write-Host "Runtime Registry not found. Creating machine-local configuration..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'bootstrap_runtime_registry.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
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
