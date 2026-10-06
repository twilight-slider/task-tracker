$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-AdminCommon.ps1')
$base = Join-Path $repo ('.runtime\tests\admin-env\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $base -Force | Out-Null
$envFile = Join-Path $base 'env.txt'
$tracker = Join-Path $base 'Tracker'
[IO.File]::WriteAllText($envFile, "TRACKER_FOLDER=$tracker`n")
if ((Read-TrackerFolderFromEnv -EnvPath $envFile) -ne $tracker) { throw 'Valid Tracker path was not read.' }
$unicodeTracker = Join-Path $base 'Трекер'
[IO.File]::WriteAllText($envFile, "TRACKER_FOLDER=$unicodeTracker`n", [Text.UTF8Encoding]::new($false))
if ((Read-TrackerFolderFromEnv -EnvPath $envFile) -cne $unicodeTracker) { throw 'UTF-8 Tracker path was not read.' }
foreach ($body in @("TRACKER_FOLDER=`n", "TRACKER_FOLDER=relative`n", "TRACKER_FOLDER=$tracker`nTRACKER_FOLDER=$tracker`n")) {
    [IO.File]::WriteAllText($envFile, $body)
    $rejected = $false
    try { Read-TrackerFolderFromEnv -EnvPath $envFile | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid TRACKER_FOLDER was accepted.' }
}
Remove-Item -LiteralPath $envFile
$rejected = $false
try { Read-TrackerFolderFromEnv -EnvPath $envFile | Out-Null } catch { $rejected = $_.Exception.Message -like '*missing*' }
if (-not $rejected) { throw 'Missing env file was accepted.' }
if (Test-TrustedTrackerBoundarySid -Sid 'S-1-5-11' -ServiceSid $null -TrustAuthenticatedUsers $false) {
    throw 'Authenticated Users unexpectedly trusted by default.'
}
foreach ($sid in @('S-1-5-11', 'S-1-5-21-1-2-3-1001')) {
    if (-not (Test-TrustedTrackerBoundarySid -Sid $sid -ServiceSid $null -TrustAuthenticatedUsers $true)) {
        throw "Explicit authenticated-user trust did not include $sid."
    }
}
foreach ($sid in @('S-1-1-0', 'S-1-5-7')) {
    if (Test-TrustedTrackerBoundarySid -Sid $sid -ServiceSid $null -TrustAuthenticatedUsers $true) {
        throw "Anonymous/Everyone principal was trusted: $sid."
    }
}
Write-Output 'TargetUser env validation checks passed'
