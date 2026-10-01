[CmdletBinding()]
param(
    [switch]$FixBaseAutoActivate
)

$ErrorActionPreference = 'Stop'

function Get-CommandPaths([string]$Name) {
    return @(Get-Command $Name -All -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandType -eq 'Application' } |
        ForEach-Object { $_.Source } |
        Where-Object { $_ } |
        Select-Object -Unique)
}

function Get-FirstLine([string]$Exe, [string[]]$Args) {
    try {
        return [string]((& $Exe @Args 2>&1 | Select-Object -First 1))
    } catch {
        return ''
    }
}

Write-Host 'Character Voice Service - environment doctor' -ForegroundColor Cyan
Write-Host ''

$condaActive = [string]$env:CONDA_PREFIX
$condaDefault = [string]$env:CONDA_DEFAULT_ENV
if ($condaActive) {
    Write-Host ("Active Conda environment: {0} ({1})" -f $condaDefault, $condaActive) -ForegroundColor Yellow
    if ($condaDefault -eq 'base') {
        Write-Host 'WARNING: Conda base is active. It can prepend old ffmpeg/python binaries to PATH for unrelated tools.' -ForegroundColor Yellow
    }
} else {
    Write-Host 'Conda environment: not active' -ForegroundColor Green
}

Write-Host ''
Write-Host 'ffmpeg candidates:' -ForegroundColor Cyan
$ffmpegs = Get-CommandPaths 'ffmpeg.exe'
if (-not $ffmpegs.Count) {
    Write-Host '  none' -ForegroundColor Yellow
} else {
    foreach ($path in $ffmpegs) {
        $version = Get-FirstLine $path @('-version')
        $tag = if ($path -match '(?i)\\(?:mini)?conda\d*\\|\\anaconda\d*\\|\\envs\\') { ' [Conda]' } else { '' }
        Write-Host ("  {0}{1}" -f $path, $tag)
        if ($version) { Write-Host ("    {0}" -f $version.Trim()) }
    }
}

Write-Host ''
Write-Host 'Python candidates:' -ForegroundColor Cyan
foreach ($path in (Get-CommandPaths 'python.exe')) {
    $version = Get-FirstLine $path @('--version')
    $tag = if ($path -match '(?i)\\(?:mini)?conda\d*\\|\\anaconda\d*\\|\\envs\\') { ' [Conda]' } else { '' }
    Write-Host ("  {0}{1} - {2}" -f $path, $tag, $version.Trim())
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$cvsPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
Write-Host ''
if (Test-Path -LiteralPath $cvsPython -PathType Leaf) {
    Write-Host ("CVS runtime: {0}" -f $cvsPython) -ForegroundColor Green
    Write-Host ("  {0}" -f (Get-FirstLine $cvsPython @('--version')).Trim())
} else {
    Write-Host 'CVS .venv is missing. Run scripts\first_setup.cmd.' -ForegroundColor Yellow
}

$indexRoot = if ($env:INDEX_TTS_ROOT) { [string]$env:INDEX_TTS_ROOT } else { Join-Path (Split-Path $projectRoot -Parent) 'index-tts' }
$indexPython = Join-Path $indexRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $indexPython -PathType Leaf) {
    Write-Host ("IndexTTS runtime: {0}" -f $indexPython) -ForegroundColor Green
    Write-Host ("  {0}" -f (Get-FirstLine $indexPython @('--version')).Trim())
} else {
    Write-Host ("IndexTTS .venv not found at {0}" -f $indexPython) -ForegroundColor DarkYellow
}


$runtimeRegistry = Join-Path $projectRoot 'config\runtimes.local.json'
Write-Host ''
if (Test-Path -LiteralPath $runtimeRegistry -PathType Leaf) {
    Write-Host ("Runtime Registry: {0}" -f $runtimeRegistry) -ForegroundColor Green
    try {
        $registry = Get-Content -LiteralPath $runtimeRegistry -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($property in @($registry.engines.PSObject.Properties)) {
            $engine = $property.Name
            $spec = $property.Value
            $enabled = if ($spec.enabled) { 'enabled' } else { 'disabled' }
            Write-Host ("  {0}: {1}" -f $engine, $enabled)
            if ($spec.executable) { Write-Host ("    executable: {0}" -f $spec.executable) }
            if ($spec.cwd) { Write-Host ("    cwd: {0}" -f $spec.cwd) }
            if ($spec.health_url) { Write-Host ("    health: {0}" -f $spec.health_url) }
            if ($spec.exclusive_group) { Write-Host ("    exclusive group: {0}" -f $spec.exclusive_group) }
        }
    } catch {
        Write-Host ("  ERROR: invalid Runtime Registry: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
} else {
    Write-Host 'Runtime Registry: missing; run scripts\init_runtime_registry.ps1' -ForegroundColor Yellow
}

if ($FixBaseAutoActivate) {
    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if (-not $conda) {
        throw 'conda command not found; cannot change auto_activate_base.'
    }
    & conda config --set auto_activate_base false
    if ($LASTEXITCODE -ne 0) { throw 'conda config failed.' }
    Write-Host ''
    Write-Host 'Conda base auto-activation disabled. Open a new terminal for the change to take effect.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'Policy:' -ForegroundColor Cyan
Write-Host '  - CVS is launched with its absolute .venv\Scripts\python.exe.'
Write-Host '  - IndexTTS is launched with its own absolute .venv\Scripts\python.exe.'
Write-Host '  - Do not install TTS engine dependencies into Conda base.'
Write-Host '  - System FFmpeg and engine-local Python environments are separate concerns.'
