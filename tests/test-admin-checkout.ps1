$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-AdminCommon.ps1')
$base = Join-Path $repo ('.runtime\tests\admin-checkout\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $base -Force | Out-Null
$work = Join-Path $base 'work'
& git init -q -b main $work
& git -C $work config user.name Test
& git -C $work config user.email test@example.invalid
[IO.File]::WriteAllText((Join-Path $work 'README.md'), 'fixture')
& git -C $work add README.md
& git -C $work commit -qm initial
& git -C $work update-ref refs/remotes/origin/main HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not create Git fixture.' }
$previousGlobalConfig = [Environment]::GetEnvironmentVariable('GIT_CONFIG_GLOBAL')
$previousDifferentOwner = [Environment]::GetEnvironmentVariable('GIT_TEST_ASSUME_DIFFERENT_OWNER')
try {
    $env:GIT_CONFIG_GLOBAL = Join-Path $base 'gitconfig'
    $env:GIT_TEST_ASSUME_DIFFERENT_OWNER = '1'
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $probe = & git -C $work rev-parse --show-toplevel 2>&1
        $probeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousErrorAction }
    $ownershipSimulated = $probeExit -ne 0 -and ($probe | Out-String) -match 'detected dubious ownership'
    Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null
    if (Test-Path -LiteralPath $env:GIT_CONFIG_GLOBAL) { throw 'Git trust changed the global Git config.' }
    if (-not $ownershipSimulated) { Write-Output 'Git ownership simulation unavailable; scoped trust was not exercised.' }
} finally {
    [Environment]::SetEnvironmentVariable('GIT_CONFIG_GLOBAL', $previousGlobalConfig, 'Process')
    [Environment]::SetEnvironmentVariable('GIT_TEST_ASSUME_DIFFERENT_OWNER', $previousDifferentOwner, 'Process')
}
$head = Assert-TaskTrackerCheckout -RepositoryRoot $work
if ($head -notmatch '^[0-9a-f]{40}$') { throw 'Clean main was not accepted.' }
[IO.File]::WriteAllText((Join-Path $work 'untracked.txt'), 'dirty')
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Untracked file was accepted.' }
catch { if ($_.Exception.Message -eq 'Untracked file was accepted.') { throw } }
Remove-Item -LiteralPath (Join-Path $work 'untracked.txt')
[IO.File]::AppendAllText((Join-Path $work 'README.md'), 'unstaged')
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Unstaged edit was accepted.' }
catch { if ($_.Exception.Message -eq 'Unstaged edit was accepted.') { throw } }
& git -C $work add README.md
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Staged edit was accepted.' }
catch { if ($_.Exception.Message -eq 'Staged edit was accepted.') { throw } }
& git -C $work commit -qm staged
& git -C $work update-ref refs/remotes/origin/main HEAD
& git -C $work checkout -q -b other
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Other branch was accepted.' }
catch { if ($_.Exception.Message -eq 'Other branch was accepted.') { throw } }
& git -C $work checkout -q main
[IO.File]::AppendAllText((Join-Path $work 'README.md'), 'changed')
& git -C $work commit -qam ahead
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Ahead commit was accepted.' }
catch { if ($_.Exception.Message -eq 'Ahead commit was accepted.') { throw } }
Write-Output 'Administrative Git trust, main/origin and clean-tree checks passed'
