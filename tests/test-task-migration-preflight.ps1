$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path $repo ('.runtime\tests\test-task-migration-preflight\run-' + $PID)
$tracker = Join-Path $testRoot 'Tracker'
$tasks = Join-Path $tracker 'tasks'
$script = Join-Path $repo 'scripts\Migrate-TrackerTasks.ps1'
try {
    New-Item -ItemType Directory -Path (Join-Path $tasks '2026\TEST-1\input') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tasks '2026\TEST-1-validation') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tasks '2027') -Force | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $config = Join-Path $testRoot 'service.json'
    @{ schemaVersion = 1; trackerRoot = $tracker; tasksRoot = $tasks; protectedRoot = (Join-Path $tracker '.protected');
       serviceAccountSid = $sid; agentSid = $sid; nodePath = (Get-Command node.exe).Source } |
        ConvertTo-Json | Set-Content -LiteralPath $config
    $result = & $script -ConfigPath $config
    if (-not @($result | Where-Object { $_ -match 'READY: 2 task folders \(1 nonstandard\); 6 directories;' }).Count -or
        -not @($result | Where-Object { $_ -match 'NONSTANDARD: .*TEST-1-validation$' }).Count) {
        throw "Unexpected migration plan: $result"
    }
    $rejected = $false
    try { & $script -ConfigPath $config -Apply | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Non-elevated task migration was accepted.' }
    Set-Content -LiteralPath (Join-Path $tasks 'AGENTS.md') -Value 'old instructions'
    $rejected = $false
    try { & $script -ConfigPath $config | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Migration accepted tasks/AGENTS.md before relocation.' }
    $installed = Join-Path $tracker '.protected\service.json'
    New-Item -ItemType Directory -Path (Split-Path -Parent $installed) -Force | Out-Null
    Copy-Item -LiteralPath $config -Destination $installed
    Set-Content -LiteralPath (Join-Path $tracker 'projects.json') -Value '{"schema_version":1,"projects":[{"project_key":"TEST","source_type":"JIRA_CLOUD","jira_host":"https://jira.example.test"}]}'
    $importPlan = & $script -ConfigPath $installed -ImportExisting
    if (-not @($importPlan | Where-Object { $_ -match '^READY: 2 task folders' }).Count) {
        throw "Existing-task import preview failed: $importPlan"
    }
    $manifestPath = Join-Path $tracker 'projects.json'
    [IO.File]::WriteAllText($manifestPath, ([char]0xFEFF + (Get-Content -LiteralPath $manifestPath -Raw)),
        [Text.UTF8Encoding]::new($false))
    if (-not @(& $script -ConfigPath $installed -ImportExisting | Where-Object { $_ -match '^READY:' }).Count) {
        throw 'BOM-prefixed manifest was rejected.'
    }
    Set-Content -LiteralPath (Join-Path $tracker 'projects.json') -Value '{"schema_version":1,"projects":[{"project_key":"OTHER","source_type":"JIRA_CLOUD","jira_host":"https://jira.example.test"}]}'
    try { & $script -ConfigPath $installed -ImportExisting | Out-Null; throw 'Unregistered task project was accepted.' }
    catch { if ($_.Exception.Message -notmatch 'unregistered project keys') { throw } }
    Write-Output 'Task migration preflight passed'
} finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
