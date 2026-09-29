$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run under the ordinary agent token, without elevation.'
}
$service = Get-CimInstance Win32_Service -Filter "Name='TaskFolderMcp'"
if ($service.State -ne 'Running' -or $service.StartName -notmatch '(?i)(^|\\)TaskFolderMcpSvc$') {
    throw 'TaskFolderMcp is not running under TaskFolderMcpSvc.'
}
$root = 'D:\Projects\Tracker\tasks'
$protected = 'C:\ProgramData\TaskFolderMcp\protected'
$requests = @(
    @{ jsonrpc = '2.0'; id = 1; method = 'tools/call'; params = @{ name = 'get_tasks_folder'; arguments = @{} } }
    @{ jsonrpc = '2.0'; id = 2; method = 'tools/call'; params = @{ name = 'get_task_projects'; arguments = @{} } }
    @{ jsonrpc = '2.0'; id = 3; method = 'tools/call'; params = @{ name = 'resolve_task_folder'; arguments = @{ key = 'AIDEV-58' } } }
) | ForEach-Object { $_ | ConvertTo-Json -Compress -Depth 8 }
$responses = @($requests | & 'C:\Program Files\nodejs\node.exe' 'C:\Program Files\TaskFolderMcp\mcp-adapter.js' |
    ForEach-Object { $_ | ConvertFrom-Json })
if ($responses.Count -ne 3 -or @($responses | Where-Object { $_.result.isError }).Count) {
    throw 'Production MCP read checks failed.'
}
$byId = @{}
foreach ($response in $responses) { $byId[[int]$response.id] = $response.result.structuredContent }
if ($byId[1].tasksFolder -ne $root -or $byId[2].tasksFolder -ne $root) {
    throw "Unexpected production root: $($byId[1].tasksFolder) / $($byId[2].tasksFolder)"
}
if ($byId[2].status -ne 'found' -or
    @($byId[2].manifest.projects | Where-Object {
        $_.project_key -eq 'AIDEV' -and $_.source_type -eq 'JIRA_CLOUD' -and
        $_.jira_host -eq 'https://twilight-slider.atlassian.net'
    }).Count -ne 1) {
    throw 'Production AIDEV registry is wrong.'
}
if ($byId[3].taskFolder -ne (Join-Path $root '2026\AIDEV-58')) {
    throw "AIDEV-58 resolved to unexpected folder: $($byId[3].taskFolder)"
}
try {
    $null = [IO.File]::ReadAllText((Join-Path $protected 'projects.json'))
    throw 'Agent can read the protected project registry.'
} catch [UnauthorizedAccessException] { }
$probe = Join-Path $protected "agent-write-probe-$PID.tmp"
$wrote = $false
try {
    [IO.File]::WriteAllText($probe, 'probe')
    $wrote = $true
} catch [UnauthorizedAccessException] { }
if ($wrote) {
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    throw 'Agent can write to protected storage.'
}
Write-Output 'Production service, AIDEV resolution, and protected-root access checks passed'
