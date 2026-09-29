$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path $repo ('.runtime\tests\test-metadata-migration-preflight\run-' + $PID)
$tracker = Join-Path $testRoot 'Tracker'
$tasks = Join-Path $tracker 'tasks'
$protected = Join-Path $tracker '.protected'
$legacyProtected = Join-Path $testRoot 'legacy\protected'
$script = Join-Path $repo 'scripts\Migrate-TrackerMetadata.ps1'
try {
    foreach ($path in @($tasks, $protected, $legacyProtected)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $configPath = Join-Path $testRoot 'service.json'
    @{ schemaVersion = 1; trackerRoot = $tracker; tasksRoot = $tasks; protectedRoot = $protected;
       serviceAccountSid = $sid; agentSid = $sid } | ConvertTo-Json | Set-Content -LiteralPath $configPath
    $legacyConfigPath = Join-Path $testRoot 'legacy\service.json'
    @{ tasksRoot = $tasks; protectedRoot = $legacyProtected } | ConvertTo-Json | Set-Content -LiteralPath $legacyConfigPath
    Set-Content -LiteralPath (Join-Path $tasks 'AGENTS.md') -Value 'old instructions'
    Set-Content -LiteralPath (Join-Path $tasks 'projects.json') -Value '{"schema_version":1,"projects":[]}'
    $manifest = '{"schema_version":1,"projects":[{"project_key":"AIDEV","source_type":"JIRA_CLOUD","jira_host":"https://jira.example.test"}]}'
    Set-Content -LiteralPath (Join-Path $legacyProtected 'projects.json') -Value $manifest
    $ready = & $script -ConfigPath $configPath -LegacyConfigPath $legacyConfigPath
    if ($ready -notmatch '^READY: Tracker metadata') { throw "Unexpected metadata plan: $ready" }
    Set-Content -LiteralPath (Join-Path $tracker 'projects.json') -Value $manifest
    $rejected = $false
    try { & $script -ConfigPath $configPath -LegacyConfigPath $legacyConfigPath | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Migration accepted a non-empty destination registry.' }
    Write-Output 'Metadata migration preflight passed'
} finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
