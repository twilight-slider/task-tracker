param(
    [string]$ConfigPath = (Join-Path $HOME '.env\task-folder-mcp.json')
)

$ErrorActionPreference = 'Stop'
$service = Get-CimInstance Win32_Service -Filter "Name='TaskFolderMcp'"
if (-not $service -or $service.State -ne 'Running' -or -not $service.ProcessId) {
    throw 'Install and start TaskFolderMcp before this test.'
}
$requested = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$manifest = Join-Path $requested.protectedRoot 'projects.json'
$state = 'C:\ProgramData\TaskFolderMcp\install-state.json'
$beforeManifest = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
$beforeState = (Get-FileHash -LiteralPath $state -Algorithm SHA256).Hash
$beforePid = $service.ProcessId
$installer = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\Install-TaskFolderMcp.ps1'
$output = & $installer -ConfigPath $ConfigPath
if (-not $?) { throw 'Installer failed.' }
$after = Get-CimInstance Win32_Service -Filter "Name='TaskFolderMcp'"
if ($after.State -ne 'Running' -or $after.ProcessId -ne $beforePid) {
    throw 'Repeated installation restarted or stopped an unchanged service.'
}
if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash -ne $beforeManifest -or
    (Get-FileHash -LiteralPath $state -Algorithm SHA256).Hash -ne $beforeState) {
    throw 'Repeated installation changed the project registry or install state.'
}
if ($output -notmatch 'Already installed and running') { throw "Unexpected installer result: $output" }
Write-Output 'Installer idempotency test passed'
