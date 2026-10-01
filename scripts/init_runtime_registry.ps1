[CmdletBinding()]
param(
    [string]$GptRoot = '',
    [string]$GptPython = '',
    [string]$IndexRoot = '',
    [string]$IndexPython = '',
    [string]$SupervisorConfig = '',
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

if (-not $SupervisorConfig) {
    $SupervisorConfig = First-ExistingFile @(
        $env:CVS_SYSTEM_SUPERVISOR_CONFIG,
        (Join-Path $parentRoot 'Supervisor\supervisor.config.json'),
        (Join-Path $parentRoot 'supervisor.config.json')
    )
}

if ($SupervisorConfig -and (Test-Path -LiteralPath $SupervisorConfig -PathType Leaf)) {
    $SupervisorConfig = (Resolve-Path -LiteralPath $SupervisorConfig).Path
    Write-Host "System Supervisor config detected: $SupervisorConfig" -ForegroundColor Cyan
    $sup = Get-Content -LiteralPath $SupervisorConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    $supRoot = Split-Path -Parent $SupervisorConfig
    $controlDir = Join-Path $supRoot 'character_voice_supervisor\control\requests'

    $gptSvc = @($sup.services | Where-Object { [string]$_.key -eq 'Api' }) | Select-Object -First 1
    $indexSvc = @($sup.services | Where-Object { [string]$_.key -eq 'IndexTTS' }) | Select-Object -First 1
    if ($null -ne $gptSvc -and $null -ne $indexSvc) {
        $registry = [ordered]@{
            schema_version = 1
            engines = [ordered]@{
                'gpt-sovits' = [ordered]@{
                    runtime_id = 'gpt-sovits-local'
                    runtime_version = 'local'
                    enabled = [bool]$gptSvc.enabled
                    mode = 'external'
                    lifecycle_owner = 'system-supervisor'
                    executable = $null
                    cwd = $null
                    args = @()
                    endpoint = ('http://127.0.0.1:{0}' -f [int]$gptSvc.port)
                    health_url = if ([string]$gptSvc.health.url) { [string]$gptSvc.health.url } else { ('http://127.0.0.1:{0}/docs' -f [int]$gptSvc.port) }
                    start_on_demand = $true
                    exclusive_group = 'gpu-0'
                    startup_timeout = [double]$gptSvc.startup_timeout_sec
                    shutdown_timeout = [double]$sup.supervisor.stop_timeout_sec
                    env = @{}
                    path_prepend = @()
                    dependencies = @(
                        [ordered]@{ id='python-runtime'; kind='python-runtime'; ownership='engine-private'; path=[string]$gptSvc.command.exe },
                        [ordered]@{ id='source-tree'; kind='source-tree'; ownership='engine-private'; path=[string]$gptSvc.cwd }
                    )
                    external_control = [ordered]@{
                        mode = 'file'
                        request_dir = $controlDir
                        service_key = 'Api'
                    }
                }
                'index-tts' = [ordered]@{
                    runtime_id = 'index-tts-2.5-local'
                    runtime_version = '2.5'
                    enabled = [bool]$indexSvc.enabled
                    mode = 'external'
                    lifecycle_owner = 'system-supervisor'
                    executable = $null
                    cwd = $null
                    args = @()
                    endpoint = ('http://127.0.0.1:{0}' -f [int]$indexSvc.port)
                    health_url = if ([string]$indexSvc.health.url) { [string]$indexSvc.health.url } else { ('http://127.0.0.1:{0}/health' -f [int]$indexSvc.port) }
                    start_on_demand = $true
                    exclusive_group = 'gpu-0'
                    startup_timeout = [double]$indexSvc.startup_timeout_sec
                    shutdown_timeout = [double]$sup.supervisor.stop_timeout_sec
                    env = @{}
                    path_prepend = @()
                    dependencies = @(
                        [ordered]@{ id='python-runtime'; kind='python-runtime'; ownership='engine-private'; path=[string]$indexSvc.command.exe },
                        [ordered]@{ id='source-tree'; kind='source-tree'; ownership='engine-private'; path=[string]$indexSvc.cwd },
                        [ordered]@{ id='model-store'; kind='model-store'; ownership='engine-private'; path=[string]$indexSvc.environment.INDEX_TTS_MODEL_DIR },
                        [ordered]@{ id='cvs-sidecar'; kind='sidecar'; ownership='platform-owned'; path=([string]@($indexSvc.command.args)[0]) }
                    )
                    external_control = [ordered]@{
                        mode = 'file'
                        request_dir = $controlDir
                        service_key = 'IndexTTS'
                    }
                }
            }
        }

        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
        $registry | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding UTF8
        Write-Host ''
        Write-Host "Runtime Registry written from System Supervisor: $configPath" -ForegroundColor Green
        Write-Host 'Lifecycle owner: system-supervisor' -ForegroundColor Green
        Write-Host 'CVS will request engine switches through the Supervisor control bridge instead of spawning engine processes itself.'
        exit 0
    }
    Write-Warning 'Supervisor config was found, but Api/IndexTTS services were not both present. Falling back to direct runtime discovery.'
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
            runtime_id = 'gpt-sovits-local'
            runtime_version = 'local'
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
            dependencies = @(
                [ordered]@{ id='python-runtime'; kind='python-runtime'; ownership='engine-private'; path=$(if($GptPython){$GptPython}else{'SET_GPT_SOVITS_PYTHON'}) },
                [ordered]@{ id='source-tree'; kind='source-tree'; ownership='engine-private'; path=$(if($GptRoot){$GptRoot}else{'SET_GPT_SOVITS_ROOT'}) }
            )
        }
        'index-tts' = [ordered]@{
            runtime_id = 'index-tts-2.5-local'
            runtime_version = '2.5'
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
            dependencies = @(
                [ordered]@{ id='python-runtime'; kind='python-runtime'; ownership='engine-private'; path=$(if($IndexPython){$IndexPython}else{'SET_INDEX_TTS_PYTHON'}) },
                [ordered]@{ id='source-tree'; kind='source-tree'; ownership='engine-private'; path=$(if($IndexRoot){$IndexRoot}else{'SET_INDEX_TTS_ROOT'}) },
                [ordered]@{ id='model-store'; kind='model-store'; ownership='engine-private'; path=$(if($IndexRoot){Join-Path $IndexRoot 'checkpoints'}else{'SET_INDEX_TTS_MODEL_DIR'}) },
                [ordered]@{ id='cvs-sidecar'; kind='sidecar'; ownership='platform-owned'; path=$indexSidecar }
            )
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
