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

function Resolve-ConfigPath([string]$Base, [string]$Value, [string]$Fallback) {
    $candidate = if ($Value) { $Value } else { $Fallback }
    $candidate = [Environment]::ExpandEnvironmentVariables([string]$candidate)
    if ([IO.Path]::IsPathRooted($candidate)) { return [IO.Path]::GetFullPath($candidate) }
    return [IO.Path]::GetFullPath((Join-Path $Base $candidate))
}

function Write-RuntimeRegistry([object]$Registry) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    $json = $Registry | ConvertTo-Json -Depth 16
    $temp = $configPath + '.tmp'
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temp, $json + [Environment]::NewLine, $utf8NoBom)
    Move-Item -LiteralPath $temp -Destination $configPath -Force
}

if ((Test-Path -LiteralPath $configPath -PathType Leaf) -and -not $Force) {
    Write-Host "Runtime Registry already exists: $configPath" -ForegroundColor Yellow
    Write-Host 'Use -Force to regenerate it.'
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
    if ($null -eq $sup -or $null -eq $sup.services) {
        throw "Supervisor config does not contain services: $SupervisorConfig"
    }

    $supRoot = Split-Path -Parent $SupervisorConfig
    $bridge = $sup.supervisor.control_bridge
    $requestSetting = if ($null -ne $bridge -and [string]$bridge.request_dir) { [string]$bridge.request_dir } else { 'character_voice_supervisor\control\requests' }
    $statusSetting = if ($null -ne $bridge -and [string]$bridge.status_file) { [string]$bridge.status_file } else { 'character_voice_supervisor\control\status.json' }
    $controlDir = Resolve-ConfigPath $supRoot $requestSetting 'character_voice_supervisor\control\requests'
    $statusFile = Resolve-ConfigPath $supRoot $statusSetting 'character_voice_supervisor\control\status.json'

    $slotMembers = @()
    if ($null -ne $sup.engine_slot) {
        $slotMembers = @($sup.engine_slot.members | ForEach-Object { [string]$_ })
    }

    $runtimeServices = @(
        $sup.services |
            Where-Object {
                $null -ne $_.system_identity -and
                [string]$_.system_identity.entity_kind -eq 'runtime' -and
                [string]$_.system_identity.engine_id
            }
    )

    if ($runtimeServices.Count -gt 0) {
        $engines = [ordered]@{}
        foreach ($svc in $runtimeServices) {
            $serviceKey = [string]$svc.key
            $engineId = [string]$svc.system_identity.engine_id
            if ($engines.Contains($engineId)) {
                throw "Supervisor config declares duplicate runtime engine_id '$engineId'."
            }

            $runtimeId = if ([string]$svc.system_identity.runtime_id) { [string]$svc.system_identity.runtime_id } else { "$engineId-local" }
            $runtimeVersion = if ([string]$svc.system_identity.runtime_version) { [string]$svc.system_identity.runtime_version } else { 'local' }
            $port = if ($null -ne $svc.port) { [int]$svc.port } else { $null }
            $endpoint = if ($null -ne $port) { "http://127.0.0.1:$port" } else { $null }

            $healthUrl = $null
            if ($null -ne $svc.health -and [string]$svc.health.url) {
                $healthUrl = [string]$svc.health.url
            }
            elseif ($endpoint) {
                if ($engineId -eq 'gpt-sovits') { $healthUrl = "$endpoint/docs" }
                else { $healthUrl = "$endpoint/health" }
            }
            if ([bool]$svc.enabled -and -not $healthUrl) {
                throw "Enabled runtime '$engineId' requires an HTTP health URL or a port."
            }

            $dependencies = @()
            if ($null -ne $svc.owned_dependencies) {
                $dependencies = @($svc.owned_dependencies)
            }
            else {
                if ($null -ne $svc.command -and [string]$svc.command.exe) {
                    $dependencies += [ordered]@{
                        id = 'python-runtime'
                        kind = 'python-runtime'
                        ownership = 'engine-private'
                        path = [string]$svc.command.exe
                    }
                }
                if ([string]$svc.cwd) {
                    $dependencies += [ordered]@{
                        id = 'source-tree'
                        kind = 'source-tree'
                        ownership = 'engine-private'
                        path = [string]$svc.cwd
                    }
                }
            }

            $isSlotMember = ($slotMembers -contains $serviceKey)
            $exclusiveGroup = if ($isSlotMember) { 'gpu-0' } else { $null }
            $startupTimeout = if ($svc.startup_timeout_sec) { [double]$svc.startup_timeout_sec } else { 120.0 }
            $shutdownTimeout = if ($sup.supervisor.stop_timeout_sec) { [double]$sup.supervisor.stop_timeout_sec } else { 15.0 }

            $engines[$engineId] = [ordered]@{
                runtime_id = $runtimeId
                runtime_version = $runtimeVersion
                enabled = [bool]$svc.enabled
                mode = 'external'
                lifecycle_owner = 'system-supervisor'
                executable = $null
                cwd = $null
                args = @()
                endpoint = $endpoint
                health_url = $healthUrl
                start_on_demand = [bool]$isSlotMember
                exclusive_group = $exclusiveGroup
                startup_timeout = $startupTimeout
                shutdown_timeout = $shutdownTimeout
                env = @{}
                path_prepend = @()
                dependencies = $dependencies
                external_control = [ordered]@{
                    mode = 'file'
                    request_dir = $controlDir
                    status_file = $statusFile
                    service_key = $serviceKey
                }
            }
        }

        $registry = [ordered]@{
            schema_version = 1
            engines = $engines
        }
        Write-RuntimeRegistry $registry

        Write-Host ''
        Write-Host "Runtime Registry written from System Supervisor: $configPath" -ForegroundColor Green
        Write-Host 'Lifecycle owner: system-supervisor' -ForegroundColor Green
        Write-Host ("Registered runtime engines: {0}" -f (($engines.Keys | Sort-Object) -join ', '))
        Write-Host 'CVS will use the Supervisor control bridge for engine-slot activation.'
        return
    }

    Write-Warning 'Supervisor config was found, but it contains no runtime system_identity entries. Falling back to direct GPT-SoVITS/IndexTTS discovery.'
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

$gptPythonPath = if ($GptPython) { $GptPython } else { 'SET_GPT_SOVITS_PYTHON' }
$gptRootPath = if ($GptRoot) { $GptRoot } else { 'SET_GPT_SOVITS_ROOT' }
$indexPythonPath = if ($IndexPython) { $IndexPython } else { 'SET_INDEX_TTS_PYTHON' }
$indexRootPath = if ($IndexRoot) { $IndexRoot } else { 'SET_INDEX_TTS_ROOT' }
$indexModelPath = if ($IndexRoot) { Join-Path $IndexRoot 'checkpoints' } else { 'SET_INDEX_TTS_MODEL_DIR' }

$registry = [ordered]@{
    schema_version = 1
    engines = [ordered]@{
        'gpt-sovits' = [ordered]@{
            runtime_id = 'gpt-sovits-local'
            runtime_version = 'local'
            enabled = $gptEnabled
            mode = 'managed'
            lifecycle_owner = 'cvs'
            executable = $gptPythonPath
            cwd = $gptRootPath
            args = @('api_v2.py', '-a', '127.0.0.1', '-p', '9880')
            endpoint = 'http://127.0.0.1:9880'
            health_url = 'http://127.0.0.1:9880/docs'
            start_on_demand = $gptEnabled
            exclusive_group = 'gpu-0'
            startup_timeout = 180
            shutdown_timeout = 20
            env = @{}
            path_prepend = @()
            dependencies = @(
                [ordered]@{ id='python-runtime'; kind='python-runtime'; ownership='engine-private'; path=$gptPythonPath },
                [ordered]@{ id='source-tree'; kind='source-tree'; ownership='engine-private'; path=$gptRootPath }
            )
        }
        'index-tts' = [ordered]@{
            runtime_id = 'index-tts-2.5-local'
            runtime_version = '2.5'
            enabled = $indexEnabled
            mode = 'managed'
            lifecycle_owner = 'cvs'
            executable = $indexPythonPath
            cwd = $indexRootPath
            args = @($indexSidecar)
            endpoint = 'http://127.0.0.1:9882'
            health_url = 'http://127.0.0.1:9882/health'
            start_on_demand = $indexEnabled
            exclusive_group = 'gpu-0'
            startup_timeout = 240
            shutdown_timeout = 20
            env = [ordered]@{
                INDEX_TTS_MODEL_DIR = $indexModelPath
                INDEX_TTS_HOST = '127.0.0.1'
                INDEX_TTS_PORT = '9882'
                INDEX_TTS_USE_BF16 = '1'
                INDEX_TTS_USE_QWEN_EMO = '0'
            }
            path_prepend = @()
            dependencies = @(
                [ordered]@{ id='python-runtime'; kind='python-runtime'; ownership='engine-private'; path=$indexPythonPath },
                [ordered]@{ id='source-tree'; kind='source-tree'; ownership='engine-private'; path=$indexRootPath },
                [ordered]@{ id='model-store'; kind='model-store'; ownership='engine-private'; path=$indexModelPath },
                [ordered]@{ id='cvs-sidecar'; kind='sidecar'; ownership='platform-owned'; path=$indexSidecar }
            )
        }
    }
}

Write-RuntimeRegistry $registry

Write-Host ''
Write-Host "Runtime Registry written: $configPath" -ForegroundColor Green
Write-Host ("GPT-SoVITS: {0}" -f $(if($gptEnabled){'managed / enabled'}else{'not detected / disabled'}))
Write-Host ("IndexTTS:    {0}" -f $(if($indexEnabled){'managed / enabled'}else{'not detected / disabled'}))
Write-Host ''
Write-Host 'Review the JSON if either runtime was not detected. The file is git-ignored and remains machine-local.'
