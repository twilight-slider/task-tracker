$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-AdminCommon.ps1')
$base = Join-Path $repo ('.runtime\tests\admin-checkout\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $base -Force | Out-Null
$origin = Join-Path $base 'origin.git'
$work = Join-Path $base 'work'
& git init --bare -q $origin
& git init -q -b main $work
& git -C $work config user.name Test
& git -C $work config user.email test@example.invalid
[IO.File]::WriteAllText((Join-Path $work 'README.md'), 'fixture')
& git -C $work add README.md
& git -C $work commit -qm initial
& git -C $work remote add origin $origin
& git -C $work push -q -u origin main
if ($LASTEXITCODE -ne 0) { throw 'Could not create Git fixture.' }
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
& git -C $work reset -q --hard HEAD
& git -C $work checkout -q -b other
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Other branch was accepted.' }
catch { if ($_.Exception.Message -eq 'Other branch was accepted.') { throw } }
& git -C $work checkout -q main
[IO.File]::AppendAllText((Join-Path $work 'README.md'), 'changed')
& git -C $work commit -qam ahead
try { Assert-TaskTrackerCheckout -RepositoryRoot $work | Out-Null; throw 'Ahead commit was accepted.' }
catch { if ($_.Exception.Message -eq 'Ahead commit was accepted.') { throw } }
Write-Output 'Administrative Git main/origin and clean-tree checks passed'
