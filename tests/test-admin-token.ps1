$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-TokenCore.ps1')
$first = New-TrackerMcpToken
$second = New-TrackerMcpToken
if ($first -eq $second -or $first -notmatch '^[A-Za-z0-9_-]{43}$' -or $second -notmatch '^[A-Za-z0-9_-]{43}$') {
    throw 'MCP token generation is invalid.'
}
$envText = Set-TrackerEnvToken -EnvText "TRACKER_FOLDER=D:\Tracker`n" -Token $first
if ((Get-TrackerEnvToken -EnvText $envText) -cne $first) { throw 'First token was not added.' }
Assert-TrackerTokenPair -ProtectedToken $first -EnvToken (Get-TrackerEnvToken -EnvText $envText)
$rotated = Set-TrackerEnvToken -EnvText $envText -Token $second
if ((Get-TrackerEnvToken -EnvText $rotated) -cne $second -or $rotated -notmatch 'TRACKER_FOLDER=D:\\Tracker') {
    throw 'Rotation did not preserve other settings.'
}
foreach ($pair in @(
    @{ Protected = $first; Env = $second },
    @{ Protected = $first; Env = $null },
    @{ Protected = $null; Env = $first }
)) {
    $rejected = $false
    try { Assert-TrackerTokenPair -ProtectedToken $pair.Protected -EnvToken $pair.Env } catch { $rejected = $true }
    if (-not $rejected) { throw 'Mismatched token copies were accepted.' }
}
$rejected = $false
try { Get-TrackerEnvToken -EnvText "TRACKER_MCP_TOKEN=a`nTRACKER_MCP_TOKEN=b`n" | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'Duplicate token entries were accepted.' }
foreach ($port in @(0, 1, 38772, 65535, 65536)) {
    if ($port -ge 1 -and $port -le 65535) {
        $text = Set-TrackerEnvPort -EnvText $envText -Port $port
        if ((Get-TrackerEnvPort -EnvText $text) -ne $port) { throw 'MCP port roundtrip failed.' }
    } else {
        $rejected = $false
        try { Set-TrackerEnvPort -EnvText $envText -Port $port | Out-Null } catch { $rejected = $true }
        if (-not $rejected) { throw 'Invalid MCP port was accepted.' }
    }
}
$rejected = $false
try { Get-TrackerEnvPort -EnvText "TRACKER_MCP_PORT=38772`nTRACKER_MCP_PORT=38773`n" | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'Duplicate MCP ports were accepted.' }
foreach ($text in @('TRACKER_MCP_TOKEN=x', 'TRACKER_MCP_PORT=abc', 'TRACKER_MCP_PORT=65536')) {
    $rejected = $false
    try { Get-TrackerEnvPort -EnvText $text | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid MCP port entry was accepted.' }
}
Write-Output 'MCP token create, preserve, rotate and mismatch checks passed'
