$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-AdminCommon.ps1')
. (Join-Path $repo 'scripts\TaskTracker-InstallLayout.ps1')
$base = Join-Path $repo ('.runtime\tests\install-layout\run-' + [guid]::NewGuid().ToString('N'))
function New-Fixture([string]$Name, [bool]$Existing) {
    $root = Join-Path $base $Name
    $protected = Join-Path $root '.protected'
    $backup = Join-Path $protected '.rollback-test'
    New-Item -ItemType Directory -Path $protected, $backup -Force | Out-Null
    if ($Existing) {
        foreach ($name in @('bin', 'runtime')) {
            $dir = Join-Path $protected $name
            New-Item -ItemType Directory -Path $dir | Out-Null
            [IO.File]::WriteAllText((Join-Path $dir 'old.txt'), $name)
        }
        [IO.File]::WriteAllText((Join-Path $protected 'service.json'), 'old')
    }
    return [pscustomobject]@{ Root = $root; Protected = $protected; Backup = $backup }
}
function Assert-Old([object]$Fixture) {
    foreach ($name in @('bin', 'runtime')) {
        if ([IO.File]::ReadAllText((Join-Path $Fixture.Protected "$name\old.txt")) -cne $name) {
            throw "Old $name was not restored."
        }
    }
    if ([IO.File]::ReadAllText((Join-Path $Fixture.Protected 'service.json')) -cne 'old') {
        throw 'Old service.json was not restored.'
    }
}
$partial = New-Fixture 'partial' $true
Move-Item -LiteralPath (Join-Path $partial.Protected 'bin') -Destination (Join-Path $partial.Backup 'bin')
New-Item -ItemType Directory -Path (Join-Path $partial.Protected 'bin') | Out-Null
[IO.File]::WriteAllText((Join-Path $partial.Protected 'bin\new.txt'), 'new')
Restore-TrackerInstallLayout -ProtectedRoot $partial.Protected -BackupRoot $partial.Backup `
    -HadLive @{ bin = $true; runtime = $true } -OldConfig 'old'
Assert-Old $partial

$full = New-Fixture 'full' $true
foreach ($name in @('bin', 'runtime')) {
    Move-Item -LiteralPath (Join-Path $full.Protected $name) -Destination (Join-Path $full.Backup $name)
    New-Item -ItemType Directory -Path (Join-Path $full.Protected $name) | Out-Null
    [IO.File]::WriteAllText((Join-Path $full.Protected "$name\new.txt"), 'new')
}
[IO.File]::WriteAllText((Join-Path $full.Protected 'service.json'), 'new')
Restore-TrackerInstallLayout -ProtectedRoot $full.Protected -BackupRoot $full.Backup `
    -HadLive @{ bin = $true; runtime = $true } -OldConfig 'old'
Assert-Old $full

$first = New-Fixture 'first' $false
foreach ($name in @('bin', 'runtime')) { New-Item -ItemType Directory -Path (Join-Path $first.Protected $name) | Out-Null }
[IO.File]::WriteAllText((Join-Path $first.Protected 'service.json'), 'new')
Restore-TrackerInstallLayout -ProtectedRoot $first.Protected -BackupRoot $first.Backup `
    -HadLive @{ bin = $false; runtime = $false } -OldConfig $null
foreach ($name in @('bin', 'runtime', 'service.json')) {
    if (Test-Path -LiteralPath (Join-Path $first.Protected $name)) { throw "First-install $name was not removed." }
}
$rejected = $false
try { Remove-TrackerInstallTree -ProtectedRoot $first.Protected -Path $first.Root } catch { $rejected = $true }
if (-not $rejected) { throw 'Cleanup accepted path outside protected root.' }
$exe = Join-Path $full.Protected 'bin\TaskTrackerService.exe'
[IO.File]::WriteAllBytes($exe, [byte[]](1, 2, 3, 4))
$before = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
if (-not (Test-TrackerHostReuse -PriorConfig ([pscustomobject]@{ hostSourceSha256 = 'host-a' }) `
    -SourceHash 'host-a' -ExistingExe $exe)) { throw 'Unchanged host did not reuse EXE.' }
if (Test-TrackerHostReuse -PriorConfig ([pscustomobject]@{ hostSourceSha256 = 'host-a' }) `
    -SourceHash 'host-b' -ExistingExe $exe) { throw 'Changed host reused EXE.' }
if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -cne $before) { throw 'Host reuse check changed EXE bytes.' }
$envPath = Join-Path $first.Root 'env.txt'
$tokenPath = Join-Path $first.Protected 'mcp-token'
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=X`n", [Text.UTF8Encoding]::new($false))
$tokenState = Get-TrackerTokenRollbackState -ProtectedTokenPath $tokenPath -EnvPath $envPath
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=X`nTRACKER_MCP_TOKEN=new`n")
[IO.File]::WriteAllText($tokenPath, 'new')
Restore-TrackerTokenPair -State $tokenState
if (Test-Path -LiteralPath $tokenPath) { throw 'First-install token survived rollback.' }
if ([IO.File]::ReadAllText($envPath) -cne "TRACKER_FOLDER=X`n") { throw 'First-install env was not restored.' }
[IO.File]::WriteAllText($tokenPath, 'old')
$tokenState = Get-TrackerTokenRollbackState -ProtectedTokenPath $tokenPath -EnvPath $envPath
[IO.File]::WriteAllText($tokenPath, 'new')
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=X`nTRACKER_MCP_TOKEN=new`n")
Restore-TrackerTokenPair -State $tokenState
if ([IO.File]::ReadAllText($tokenPath) -cne 'old' -or [IO.File]::ReadAllText($envPath) -cne "TRACKER_FOLDER=X`n") {
    throw 'Existing token pair was not restored.'
}
$projects = Join-Path $base 'projects'
New-Item -ItemType Directory -Path $projects -Force | Out-Null
$roots = @(Assert-TrackerSnapshotRoots -Roots @($projects) -TrackerRoot $first.Root)
if ($roots.Count -ne 1 -or $roots[0] -ine $projects) { throw 'First-install snapshot root was not retained.' }
foreach ($forbidden in @($base, $first.Root)) {
    $rejected = $false
    try { Assert-TrackerSnapshotRoots -Roots @($forbidden) -TrackerRoot $first.Root | Out-Null }
    catch { $rejected = $true }
    if (-not $rejected) { throw "Snapshot root accepted Tracker or its ancestor: $forbidden" }
}
Write-Output 'Layout, token-pair, snapshot-root and host reuse checks passed'
