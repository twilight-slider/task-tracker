$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this test under the ordinary agent token, without elevation.'
}
$adapter = 'C:\Program Files\TaskFolderMcp\mcp-adapter.js'
$node = 'C:\Program Files\nodejs\node.exe'
$arguments = 1..3 | ForEach-Object {
    @{ project_key = 'TFMTEST'; title = "Параллельная проверка $_";
       statement = 'Тестовая задача AIDEV-58 в отдельном каталоге.';
       request_id = "aidev58-parallel-20260929-$_" }
}
$requests = for ($i = 0; $i -lt $arguments.Count; $i++) {
    @{ jsonrpc = '2.0'; id = $i + 1; method = 'tools/call'; params = @{
        name = 'create_local_task_folder'; arguments = $arguments[$i]
    } } | ConvertTo-Json -Compress -Depth 8
}
$responses = @($requests | & $node $adapter | ForEach-Object { $_ | ConvertFrom-Json })
if ($responses.Count -ne 3 -or @($responses | Where-Object { $_.result.isError -or -not $_.result.structuredContent.key }).Count) {
    throw 'At least one parallel task request failed.'
}
$keys = @($responses | ForEach-Object { $_.result.structuredContent.key })
if (@($keys | Select-Object -Unique).Count -ne 3) { throw 'Parallel requests received duplicate task keys.' }
foreach ($response in $responses) {
    $task = $response.result.structuredContent
    if (-not (Test-Path -LiteralPath $task.sourceReference)) { throw "Missing task statement: $($task.key)" }
}
$conflict = @{ jsonrpc = '2.0'; id = 4; method = 'tools/call'; params = @{
    name = 'create_local_task_folder'; arguments = @{
        project_key = 'TFMTEST'; title = 'Изменённая постановка'; statement = 'Другой запрос';
        request_id = $arguments[0].request_id
    }
} } | ConvertTo-Json -Compress -Depth 8
$reply = $conflict | & $node $adapter | ConvertFrom-Json
if ($reply.result.structuredContent.code -ne 'REQUEST_ID_CONFLICT') {
    throw 'Reusing request_id with different arguments was not rejected.'
}
Write-Output "Parallel task creation passed: $($keys -join ', ')"
