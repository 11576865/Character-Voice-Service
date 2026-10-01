[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot 'init_runtime_registry.ps1'

try {
    & $target -Force:$Force
    exit 0
}
catch {
    Write-Error ("Runtime Registry initialization failed: {0}" -f $_.Exception.Message)
    exit 1
}
