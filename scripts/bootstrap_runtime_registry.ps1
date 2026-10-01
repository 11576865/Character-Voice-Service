[CmdletBinding()]
param(
    [switch]$Force
)

# Compatibility wrapper. Runtime Registry initialization is owned by
# init_runtime_registry.ps1 so Supervisor integration, runtime identity,
# dependency ownership and future schema changes cannot drift between scripts.
$target = Join-Path $PSScriptRoot 'init_runtime_registry.ps1'
& $target -Force:$Force
exit $LASTEXITCODE
