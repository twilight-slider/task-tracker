$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path $repo ('.runtime\tests\test-task-migration-preflight\run-' + $PID)
$tracker = Join-Path $testRoot 'Tracker'
$tasks = Join-Path $tracker 'tasks'
$script = Join-Path $repo 'scripts\Migrate-TrackerTasks.ps1'
try {
    New-Item -ItemType Directory -Path (Join-Path $tasks '2026\TEST-1\input') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tasks '2027') -Force | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $config = Join-Path $testRoot 'service.json'
    @{ schemaVersion = 1; trackerRoot = $tracker; tasksRoot = $tasks; protectedRoot = (Join-Path $tracker '.protected');
       serviceAccountSid = $sid; agentSid = $sid } |
        ConvertTo-Json | Set-Content -LiteralPath $config
    $result = & $script -ConfigPath $config
    if ($result -notmatch 'READY: 1 tasks; 5 directories;') { throw "Unexpected migration plan: $result" }
    $rejected = $false
    try { & $script -ConfigPath $config -Apply | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Non-elevated task migration was accepted.' }
    Set-Content -LiteralPath (Join-Path $tasks 'AGENTS.md') -Value 'old instructions'
    $rejected = $false
    try { & $script -ConfigPath $config | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Migration accepted tasks/AGENTS.md before relocation.' }
    Write-Output 'Task migration preflight passed'
} finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
