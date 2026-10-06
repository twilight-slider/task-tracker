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
Write-Output 'MCP token create, preserve, rotate and mismatch checks passed'
