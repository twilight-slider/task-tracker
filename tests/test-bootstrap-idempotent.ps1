$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ("task-folder-bootstrap-$PID")
$config = Join-Path $root 'task-folder-mcp.json'
$testBackup = Join-Path $root 'task-folder-mcp.test.json'
$script = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\Bootstrap-TaskFolderMcp.ps1'
try {
    & $script -Target Test -ConfigPath $config | Out-Null
    $first = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash
    & $script -Target Test -ConfigPath $config | Out-Null
    if ((Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash -ne $first) {
        throw 'Second bootstrap changed the config file.'
    }

    $initial = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
    $initial.serviceAccountSid = $null
    [IO.File]::WriteAllText($config, (($initial | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
    $beforeAccountCreation = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash
    & $script -Target Test -ConfigPath $config | Out-Null
    if ((Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash -ne $beforeAccountCreation) {
        throw 'Bootstrap changed a config created before the service account existed.'
    }
    & $script -ConfigPath $config | Out-Null
    if (-not (Test-Path -LiteralPath $testBackup) -or
        (Get-FileHash -LiteralPath $testBackup -Algorithm SHA256).Hash -ne $beforeAccountCreation) {
        throw 'Migration did not preserve the previous test config.'
    }
    $production = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
    if ($production.tasksRoot -ne 'D:\Projects\Tracker\tasks' -or
        $production.protectedRoot -ne 'C:\ProgramData\TaskFolderMcp\protected') {
        throw 'Production bootstrap selected unexpected storage roots.'
    }
    $productionHash = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash
    & $script -ConfigPath $config | Out-Null
    if ((Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash -ne $productionHash) {
        throw 'Repeated production bootstrap changed the config file.'
    }
    Write-Output 'Bootstrap idempotency tests passed'
} finally {
    if (Test-Path -LiteralPath $config) { Remove-Item -LiteralPath $config -Force }
    if (Test-Path -LiteralPath $testBackup) { Remove-Item -LiteralPath $testBackup -Force }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Force }
}
