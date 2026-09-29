param(
    [string]$EnvPath = (Join-Path $HOME '.env\env.txt'),
    [string]$ConfigPath = (Join-Path $HOME '.env\task-tracker.json'),
    [string]$ServiceAccountName
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run bootstrap without elevation.'
}
if (-not [IO.Path]::IsPathFullyQualified($EnvPath) -or -not [IO.Path]::IsPathFullyQualified($ConfigPath)) {
    throw 'EnvPath and ConfigPath must be absolute.'
}
$EnvPath = [IO.Path]::GetFullPath($EnvPath)
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$envFile = Get-Item -LiteralPath $EnvPath -Force -ErrorAction Stop
if ($envFile.PSIsContainer -or ($envFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'env.txt must be a regular file.' }
$values = @(Get-Content -LiteralPath $EnvPath | Where-Object { $_ -match '^\s*TRACKER_FOLDER\s*=' })
if ($values.Count -ne 1) { throw 'env.txt must contain exactly one TRACKER_FOLDER.' }
$rawRoot = ($values[0] -split '=', 2)[1].Trim()
if (-not [IO.Path]::IsPathFullyQualified($rawRoot)) { throw 'TRACKER_FOLDER must be an absolute path.' }
$root = [IO.Path]::GetFullPath($rawRoot).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
if ($root -eq [IO.Path]::GetPathRoot($root).TrimEnd([IO.Path]::DirectorySeparatorChar)) { throw 'TRACKER_FOLDER cannot be a volume root.' }
$userName = $identity.Name.Split('\')[-1]
if (-not $ServiceAccountName) {
    $ServiceAccountName = @("$userName-Task-Tracker", "$userName-T-Tracker", "$userName-Tracker", "$userName-TT") |
        Where-Object { $_.Length -le 20 } | Select-Object -First 1
    if (-not $ServiceAccountName) { throw 'Specify -ServiceAccountName: all standard names exceed the Windows limit.' }
}
if ($ServiceAccountName.Length -gt 20 -or $ServiceAccountName -notmatch '^[\p{L}\p{N}._-]+$') {
    throw 'Invalid ServiceAccountName. Provide a Windows-compatible name of at most 20 characters.'
}
$account = Get-LocalUser -Name $ServiceAccountName -ErrorAction SilentlyContinue
$rid = $identity.User.Value.Split('-')[-1]
$config = [ordered]@{
    schemaVersion = 2
    ownerSid = $identity.User.Value
    agentSid = $identity.User.Value
    trackerRoot = $root
    tasksRoot = Join-Path $root 'tasks'
    protectedRoot = Join-Path $root '.protected'
    installRoot = Join-Path $root '.protected\bin'
    serviceName = "TaskTracker-$rid"
    serviceAccountName = $ServiceAccountName
    serviceAccountSid = if ($account) { $account.SID.Value } else { $null }
    pipeName = "task-tracker-$rid-v1"
    nodePath = (Get-Command node.exe -ErrorAction Stop).Source
}
$directory = [IO.Path]::GetDirectoryName($ConfigPath)
if (Test-Path -LiteralPath $directory) {
    $item = Get-Item -LiteralPath $directory -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Config directory must be regular.' }
} else { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
if (Test-Path -LiteralPath $ConfigPath) {
    $item = Get-Item -LiteralPath $ConfigPath -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Bootstrap config must be a regular file.' }
    $existing = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if ($existing.serviceAccountSid -and $account -and $existing.serviceAccountSid -ne $account.SID.Value) {
        throw 'Service account SID changed after bootstrap.'
    }
    $config.serviceAccountSid = $existing.serviceAccountSid
    if (($existing | ConvertTo-Json -Depth 3) -ne ($config | ConvertTo-Json -Depth 3)) {
        throw 'Existing bootstrap request differs. Moving Tracker requires a full reinstall.'
    }
    Write-Output "Already prepared: $ConfigPath"
    return
}
[IO.File]::WriteAllText($ConfigPath, (($config | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
Write-Output "Prepared: $ConfigPath; Tracker: $root"
