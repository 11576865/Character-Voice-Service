[CmdletBinding()]
param(
    [string]$OutputPath = '',
    [string]$RuntimeRegistry = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $projectRoot 'data\host-runtime-inventory.json'
}
if (-not $RuntimeRegistry) {
    $RuntimeRegistry = Join-Path $projectRoot 'config\runtimes.local.json'
}

function Get-ExeVersion([string]$Path, [string]$Kind) {
    try {
        if ($Kind -eq 'python') {
            return [string]((& $Path --version 2>&1 | Select-Object -First 1))
        }
        if ($Kind -in @('ffmpeg', 'ffprobe')) {
            return [string]((& $Path -version 2>&1 | Select-Object -First 1))
        }
        if ($Kind -eq 'conda') {
            return [string]((& $Path --version 2>&1 | Select-Object -First 1))
        }
    } catch {}
    finally { $global:LASTEXITCODE = 0 }
    return ''
}

$items = @()
$seen = @{}

function Add-InventoryItem(
    [string]$Kind,
    [string]$Path,
    [string]$Source,
    [string]$Scope = 'host-visible',
    [string]$Version = ''
) {
    if (-not $Path) { return }
    try {
        $full = [IO.Path]::GetFullPath($Path)
    } catch {
        $full = $Path
    }
    $key = ("{0}|{1}" -f $Kind, $full).ToLowerInvariant()
    if ($seen.ContainsKey($key)) { return }
    $seen[$key] = $true
    $exists = Test-Path -LiteralPath $full
    $script:items += [pscustomobject][ordered]@{
        kind = $Kind
        path = $full
        source = $Source
        scope = $Scope
        version = $Version
        exists = [bool]$exists
    }
}

foreach ($tool in @(
    @{ Name='python.exe'; Kind='python' },
    @{ Name='ffmpeg.exe'; Kind='ffmpeg' },
    @{ Name='ffprobe.exe'; Kind='ffprobe' },
    @{ Name='conda.exe'; Kind='conda' }
)) {
    foreach ($command in @(Get-Command $tool.Name -All -ErrorAction SilentlyContinue)) {
        if ($command.CommandType -ne 'Application' -or -not $command.Source) { continue }
        $version = Get-ExeVersion $command.Source $tool.Kind
        Add-InventoryItem $tool.Kind $command.Source 'PATH' 'host-visible' ([string]$version).Trim()
    }
}

$conda = Get-Command conda -ErrorAction SilentlyContinue
if ($conda) {
    try {
        $envJson = & conda env list --json 2>$null
        if ($LASTEXITCODE -eq 0 -and $envJson) {
            $parsed = ($envJson -join [Environment]::NewLine) | ConvertFrom-Json
            foreach ($prefix in @($parsed.envs)) {
                if (-not $prefix) { continue }
                $python = Join-Path ([string]$prefix) 'python.exe'
                Add-InventoryItem 'conda-env' ([string]$prefix) 'conda-env-list' 'host-visible' ''
                if (Test-Path -LiteralPath $python -PathType Leaf) {
                    $version = Get-ExeVersion $python 'python'
                    Add-InventoryItem 'python' $python 'conda-env-list' 'host-visible' ([string]$version).Trim()
                }
                foreach ($mediaTool in @('ffmpeg.exe','ffprobe.exe')) {
                    $toolPath = Join-Path ([string]$prefix) ('Library\bin\' + $mediaTool)
                    if (Test-Path -LiteralPath $toolPath -PathType Leaf) {
                        $kind = [IO.Path]::GetFileNameWithoutExtension($mediaTool)
                        $version = Get-ExeVersion $toolPath $kind
                        Add-InventoryItem $kind $toolPath 'conda-env-list' 'engine-candidate' ([string]$version).Trim()
                    }
                }
            }
        }
    } catch {}
}

if (Test-Path -LiteralPath $RuntimeRegistry -PathType Leaf) {
    try {
        $registry = Get-Content -LiteralPath $RuntimeRegistry -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($property in @($registry.engines.PSObject.Properties)) {
            $engineId = $property.Name
            $spec = $property.Value
            foreach ($dep in @($spec.dependencies)) {
                if (-not $dep -or -not $dep.path) { continue }
                $depKind = if ($dep.kind) { [string]$dep.kind } else { 'declared-dependency' }
                $depOwnership = if ($dep.ownership) { [string]$dep.ownership } else { 'unspecified' }
                $depVersion = if ($dep.version) { [string]$dep.version } else { '' }
                Add-InventoryItem $depKind ([string]$dep.path) ("runtime-registry:" + $engineId) $depOwnership $depVersion
            }
        }
    } catch {
        Write-Warning ("Could not read Runtime Registry: {0}" -f $_.Exception.Message)
    }
}

$payload = [ordered]@{
    schema_version = 1
    metadata = [ordered]@{
        generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        computer_name = $env:COMPUTERNAME
        user = $env:USERNAME
        active_conda = [string]$env:CONDA_DEFAULT_ENV
        active_conda_prefix = [string]$env:CONDA_PREFIX
        powershell = $PSVersionTable.PSVersion.ToString()
    }
    items = $items
}

$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
$temp = $OutputPath + '.tmp'
$payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temp -Encoding UTF8
Move-Item -LiteralPath $temp -Destination $OutputPath -Force

Write-Host ("Host runtime inventory written: {0}" -f $OutputPath) -ForegroundColor Green
Write-Host ("Items: {0}" -f $items.Count)

$global:LASTEXITCODE = 0
