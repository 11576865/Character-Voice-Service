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
    if (-not $Exe) { return '' }
    try {
        $line = & $Exe @Args 2>&1 | Select-Object -First 1
        if ($null -eq $line) { return '' }
        return [string]$line
    } catch {
        return ''
    }
}

function Format-VersionLine([object]$Value) {
    if ($null -eq $Value) { return '' }
    return ([string]$Value).Trim()
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
        if ($version) { Write-Host ("    {0}" -f (Format-VersionLine $version)) }
    }
}

Write-Host ''
Write-Host 'Python candidates:' -ForegroundColor Cyan
foreach ($path in (Get-CommandPaths 'python.exe')) {
    $version = Get-FirstLine $path @('--version')
    $tag = if ($path -match '(?i)\\(?:mini)?conda\d*\\|\\anaconda\d*\\|\\envs\\') { ' [Conda]' } else { '' }
    Write-Host ("  {0}{1} - {2}" -f $path, $tag, (Format-VersionLine $version))
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$cvsPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
Write-Host ''
if (Test-Path -LiteralPath $cvsPython -PathType Leaf) {
    Write-Host ("CVS runtime: {0}" -f $cvsPython) -ForegroundColor Green
    Write-Host ("  {0}" -f (Format-VersionLine (Get-FirstLine $cvsPython @('--version'))))
} else {
    Write-Host 'CVS .venv is missing. Run scripts\first_setup.cmd.' -ForegroundColor Yellow
}

$runtimeRegistry = Join-Path $projectRoot 'config\runtimes.local.json'
if (-not (Test-Path -LiteralPath $runtimeRegistry -PathType Leaf)) {
    $indexRoot = if ($env:INDEX_TTS_ROOT) { [string]$env:INDEX_TTS_ROOT } else { Join-Path (Split-Path $projectRoot -Parent) 'index-tts' }
    $indexPython = Join-Path $indexRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $indexPython -PathType Leaf) {
        Write-Host ("IndexTTS runtime: {0}" -f $indexPython) -ForegroundColor Green
        Write-Host ("  {0}" -f (Format-VersionLine (Get-FirstLine $indexPython @('--version'))))
    } else {
        Write-Host ("IndexTTS legacy probe: not found at {0}" -f $indexPython) -ForegroundColor DarkYellow
    }
}

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
            if ($spec.runtime_id) { Write-Host ("    runtime id: {0}" -f $spec.runtime_id) }
            if ($spec.mode) { Write-Host ("    mode: {0}" -f $spec.mode) }
            if ($spec.lifecycle_owner) { Write-Host ("    lifecycle owner: {0}" -f $spec.lifecycle_owner) }
            if ($spec.executable) { Write-Host ("    executable: {0}" -f $spec.executable) }
            if ($spec.cwd) { Write-Host ("    cwd: {0}" -f $spec.cwd) }
            if ($spec.endpoint) { Write-Host ("    endpoint: {0}" -f $spec.endpoint) }
            if ($spec.health_url) { Write-Host ("    health: {0}" -f $spec.health_url) }
            if ($spec.exclusive_group) { Write-Host ("    exclusive group: {0}" -f $spec.exclusive_group) }
            foreach ($dep in @($spec.dependencies)) {
                if ($null -eq $dep) { continue }
                $exists = if ($dep.path) { Test-Path -LiteralPath ([string]$dep.path) } else { $null }
                $existsText = if ($null -eq $exists) { 'n/a' } elseif ($exists) { 'exists' } else { 'MISSING' }
                Write-Host ("    dependency: {0} [{1}] {2} - {3}" -f $dep.id, $dep.ownership, $dep.path, $existsText)
            }
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
