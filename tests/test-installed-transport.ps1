param(
    [string]$AdapterPath = 'C:\Program Files\TaskFolderMcp\mcp-adapter.js'
)

$ErrorActionPreference = 'Stop'
$node = 'C:\Program Files\nodejs\node.exe'
if (-not (Test-Path -LiteralPath $AdapterPath)) { throw "MCP adapter not found: $AdapterPath" }
$requests = 1..20 | ForEach-Object {
    @{ jsonrpc = '2.0'; id = $_; method = 'tools/call'; params = @{
        name = 'get_task_projects'; arguments = @{}
    } } | ConvertTo-Json -Compress -Depth 8
}
$responses = @($requests | & $node $AdapterPath | ForEach-Object { $_ | ConvertFrom-Json })
if ($responses.Count -ne $requests.Count) { throw "Expected $($requests.Count) responses, got $($responses.Count)." }
$failed = @($responses | Where-Object { $_.result.isError -or $_.result.structuredContent.status -ne 'found' })
if ($failed.Count) {
    throw "$($failed.Count) requests failed: $($failed[0].result.structuredContent.code)"
}
Write-Output 'Installed named-pipe transport passed 20 concurrent requests'
