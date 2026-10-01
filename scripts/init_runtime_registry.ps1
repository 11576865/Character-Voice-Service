[CmdletBinding()]
param(
    [string]$GptRoot = '',
    [string]$GptPython = '',
    [string]$IndexRoot = '',
    [string]$IndexPython = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$parentRoot = Split-Path -Parent $projectRoot
$configDir = Join-Path $projectRoot 'config'
$configPath = Join-Path $configDir 'runtimes.local.json'

function First-ExistingFile([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function First-ExistingDir([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

if ((Test-Path -LiteralPath $configPath -PathType Leaf) -and -not $Force) {
    Write-Host "Runtime Registry already exists: $configPath" -ForegroundColor Yellow
    Write-Host 'Use -Force to regenerate it.' -ForegroundColor Yellow
    exit 2
}

if (-not $GptRoot) {
    $GptRoot = First-ExistingDir @(
        $env:CVS_GPT_SOVITS_ROOT,
        $env:GPT_SOVITS_ROOT,
        (Join-Path $parentRoot 'GPT-SoVITS'),
        (Join-Path $parentRoot 'GPT_SoVITS')
    )
}
if ($GptRoot) { $GptRoot = (Resolve-Path -LiteralPath $GptRoot).Path }

if (-not $GptPython -and $GptRoot) {
    $gptParent = Split-Path -Parent $GptRoot
    $GptPython = First-ExistingFile @(
        $env:CVS_GPT_SOVITS_PYTHON,
        (Join-Path $GptRoot '.venv\Scripts\python.exe'),
        (Join-Path $GptRoot 'venv\Scripts\python.exe'),
        (Join-Path $gptParent 'GPT-SoVITS-env\python.exe'),
        (Join-Path $gptParent 'GPT-SoVITS-env\Scripts\python.exe'),
        (Join-Path $GptRoot 'python.exe')
    )
}

if (-not $IndexRoot) {
    $IndexRoot = First-ExistingDir @(
        $env:INDEX_TTS_ROOT,
        (Join-Path $parentRoot 'index-tts'),
        (Join-Path $parentRoot 'IndexTTS'),
        (Join-Path $parentRoot 'IndexTTS-2.5')
    )
}
if ($IndexRoot) { $IndexRoot = (Resolve-Path -LiteralPath $IndexRoot).Path }

if (-not $IndexPython -and $IndexRoot) {
    $IndexPython = First-ExistingFile @(
        $env:INDEX_TTS_PYTHON,
        (Join-Path $IndexRoot '.venv\Scripts\python.exe'),
        (Join-Path $IndexRoot 'venv\Scripts\python.exe'),
        (Join-Path $IndexRoot 'python.exe')
    )
}

$gptApi = if ($GptRoot) { Join-Path $GptRoot 'api_v2.py' } else { '' }
$gptEnabled = [bool]($GptRoot -and $GptPython -and (Test-Path -LiteralPath $gptApi -PathType Leaf))
$indexSidecar = Join-Path $projectRoot 'sidecars\index_tts_api.py'
$indexEnabled = [bool]($IndexRoot -and $IndexPython -and (Test-Path -LiteralPath $indexSidecar -PathType Leaf))

$registry = [ordered]@{
    schema_version = 1
    engines = [ordered]@{
        'gpt-sovits' = [ordered]@{
            enabled = $gptEnabled
            mode = 'managed'
            executable = if ($GptPython) { $GptPython } else { 'SET_GPT_SOVITS_PYTHON' }
            cwd = if ($GptRoot) { $GptRoot } else { 'SET_GPT_SOVITS_ROOT' }
            args = @('api_v2.py', '-a', '127.0.0.1', '-p', '9880')
            endpoint = 'http://127.0.0.1:9880'
            health_url = 'http://127.0.0.1:9880/docs'
            start_on_demand = $true
            exclusive_group = 'gpu-0'
            startup_timeout = 180
            shutdown_timeout = 20
            env = @{}
            path_prepend = @()
        }
        'index-tts' = [ordered]@{
            enabled = $indexEnabled
            mode = 'managed'
            executable = if ($IndexPython) { $IndexPython } else { 'SET_INDEX_TTS_PYTHON' }
            cwd = if ($IndexRoot) { $IndexRoot } else { 'SET_INDEX_TTS_ROOT' }
            args = @($indexSidecar)
            endpoint = 'http://127.0.0.1:9882'
            health_url = 'http://127.0.0.1:9882/health'
            start_on_demand = $true
            exclusive_group = 'gpu-0'
            startup_timeout = 240
            shutdown_timeout = 20
            env = [ordered]@{
                INDEX_TTS_MODEL_DIR = if ($IndexRoot) { Join-Path $IndexRoot 'checkpoints' } else { 'SET_INDEX_TTS_MODEL_DIR' }
                INDEX_TTS_HOST = '127.0.0.1'
                INDEX_TTS_PORT = '9882'
                INDEX_TTS_USE_BF16 = '1'
                INDEX_TTS_USE_QWEN_EMO = '0'
            }
            path_prepend = @()
        }
    }
}

New-Item -ItemType Directory -Path $configDir -Force | Out-Null
$registry | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8

Write-Host ''
Write-Host "Runtime Registry written: $configPath" -ForegroundColor Green
Write-Host ("GPT-SoVITS: {0}" -f $(if($gptEnabled){'managed / enabled'}else{'not detected / disabled'}))
Write-Host ("IndexTTS:    {0}" -f $(if($indexEnabled){'managed / enabled'}else{'not detected / disabled'}))
Write-Host ''
Write-Host 'Review the JSON if either runtime was not detected. The file is git-ignored and remains machine-local.'
