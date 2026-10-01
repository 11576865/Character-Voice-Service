[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configDir = Join-Path $projectRoot 'config'
$output = Join-Path $configDir 'runtimes.local.json'

if ((Test-Path -LiteralPath $output -PathType Leaf) -and -not $Force) {
    Write-Host "Runtime Registry already exists: $output" -ForegroundColor Yellow
    Write-Host "Use -Force to replace it." -ForegroundColor Yellow
    exit 2
}

function First-ExistingPath([string[]]$Candidates, [switch]$Directory) {
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        try { $full = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($candidate)) }
        catch { continue }
        if ($Directory) {
            if (Test-Path -LiteralPath $full -PathType Container) { return $full }
        } else {
            if (Test-Path -LiteralPath $full -PathType Leaf) { return $full }
        }
    }
    return $null
}

$parent = Split-Path $projectRoot -Parent

$gptRoot = First-ExistingPath @(
    $env:CVS_GPT_SOVITS_ROOT,
    (Join-Path $parent 'GPT-SoVITS'),
    (Join-Path $parent 'GPT_SoVITS')
) -Directory

$gptPythonCandidates = @($env:CVS_GPT_SOVITS_PYTHON)
if ($gptRoot) {
    $gptPythonCandidates += @(
        (Join-Path $gptRoot '.venv\Scripts\python.exe'),
        (Join-Path $gptRoot 'venv\Scripts\python.exe'),
        (Join-Path $gptRoot 'runtime\python.exe'),
        (Join-Path (Split-Path $gptRoot -Parent) 'GPT-SoVITS-env\python.exe')
    )
}
$gptPython = First-ExistingPath $gptPythonCandidates

$indexRoot = First-ExistingPath @(
    $env:INDEX_TTS_ROOT,
    (Join-Path $parent 'index-tts'),
    (Join-Path $parent 'IndexTTS'),
    (Join-Path $parent 'index-tts-2.5')
) -Directory

$indexPythonCandidates = @($env:INDEX_TTS_PYTHON)
if ($indexRoot) {
    $indexPythonCandidates += @(
        (Join-Path $indexRoot '.venv\Scripts\python.exe'),
        (Join-Path $indexRoot 'venv\Scripts\python.exe'),
        (Join-Path $indexRoot 'python.exe')
    )
}
$indexPython = First-ExistingPath $indexPythonCandidates

$engines = [ordered]@{}

$gptEnabled = [bool]($gptRoot -and $gptPython)
$gptArgs = @('api_v2.py', '-a', '127.0.0.1', '-p', '9880')
$engines['gpt-sovits'] = [ordered]@{
    enabled = $gptEnabled
    mode = 'managed'
    executable = if($gptPython){$gptPython}else{'D:\path\to\GPT-SoVITS-env\python.exe'}
    cwd = if($gptRoot){$gptRoot}else{'D:\path\to\GPT-SoVITS'}
    args = $gptArgs
    endpoint = 'http://127.0.0.1:9880'
    health_url = 'http://127.0.0.1:9880/docs'
    start_on_demand = $gptEnabled
    exclusive_group = 'gpu0'
    startup_timeout = 120
    shutdown_timeout = 15
    env = @{}
    path_prepend = @()
}

$indexEnabled = [bool]($indexRoot -and $indexPython)
$indexEnv = [ordered]@{
    INDEX_TTS_HOST = '127.0.0.1'
    INDEX_TTS_PORT = '9882'
    INDEX_TTS_USE_BF16 = '1'
    INDEX_TTS_USE_QWEN_EMO = '0'
}
if ($indexRoot) {
    $indexEnv['INDEX_TTS_MODEL_DIR'] = Join-Path $indexRoot 'checkpoints'
}

$engines['index-tts'] = [ordered]@{
    enabled = $indexEnabled
    mode = 'managed'
    executable = if($indexPython){$indexPython}else{'D:\path\to\index-tts\.venv\Scripts\python.exe'}
    cwd = if($indexRoot){$indexRoot}else{'D:\path\to\index-tts'}
    args = @((Join-Path $projectRoot 'sidecars\index_tts_api.py'))
    endpoint = 'http://127.0.0.1:9882'
    health_url = 'http://127.0.0.1:9882/health'
    start_on_demand = $indexEnabled
    exclusive_group = 'gpu0'
    startup_timeout = 180
    shutdown_timeout = 15
    env = $indexEnv
    path_prepend = @()
}

[void](New-Item -ItemType Directory -Path $configDir -Force)
$payload = [ordered]@{
    schema_version = 1
    engines = $engines
}
$json = $payload | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($output, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))

Write-Host "Runtime Registry written: $output" -ForegroundColor Green
Write-Host ("  GPT-SoVITS: {0}" -f ($(if($gptEnabled){'detected'}else{'disabled - edit paths'})))
Write-Host ("  IndexTTS:   {0}" -f ($(if($indexEnabled){'detected'}else{'disabled - edit paths'})))
Write-Host ''
Write-Host 'Review config\runtimes.local.json, then run:' -ForegroundColor Cyan
Write-Host '  .\scripts\environment_doctor.ps1'
Write-Host '  .\scripts\run_server.cmd'
