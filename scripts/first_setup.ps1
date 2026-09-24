[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements.txt"

function Find-WorkingPython {
    $candidates = @(@{ Command = "py"; Arguments = @("-3") })

    foreach ($name in @("python", "python3")) {
        foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            $candidates += @{ Command = $command.Source; Arguments = @() }
        }
    }

    $pipCommand = Get-Command "pip" -ErrorAction SilentlyContinue
    if ($null -ne $pipCommand) {
        $pythonBesidePip = Join-Path (Split-Path -Parent $pipCommand.Source) "python.exe"
        if (Test-Path -LiteralPath $pythonBesidePip -PathType Leaf) {
            $candidates += @{ Command = $pythonBesidePip; Arguments = @() }
        }
    }

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
        try {
            & $candidate.Command @($candidate.Arguments) -c "import sys; assert sys.version_info >= (3, 10)" *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
        catch { }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $python = Find-WorkingPython
    if ($null -eq $python) {
        Write-Host "ERROR: Python 3.10 or newer was not found. Install Python from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH', then run this script again." -ForegroundColor Red
        exit 1
    }
    Write-Host "Creating project virtual environment at .venv ..."
    Push-Location $projectRoot
    try {
        & $python.Command @($python.Arguments) -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv (exit code $LASTEXITCODE)." }
    }
    finally { Pop-Location }
}

Write-Host "Installing project dependencies ..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install requirements.txt." }

Push-Location $projectRoot
try {
    & $venvPython -m server.voice_profiles
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally { Pop-Location }

Write-Host "Setup complete. Start the service with .\scripts\run_server.cmd"
