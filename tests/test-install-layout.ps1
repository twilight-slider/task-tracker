param([string]$InstallerPath)

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

$installer = if ($InstallerPath) { $InstallerPath } else { Join-Path $repo 'scripts\Install-TaskTrackerV3.ps1' }
$parseErrors = $null
$syntax = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "Installer syntax failed: $($parseErrors[0].Message)" }
$ownerFunction = $syntax.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Set-TrackerDirectoryAcl' }, $true)
if (-not $ownerFunction) { throw 'Installer tasks ACL function is missing.' }
$transaction = @($syntax.FindAll({ param($node) $node -is [Management.Automation.Language.TryStatementAst] -and
    $node.Body.Extent.Text.Contains('Start-Service -Name $serviceName') -and
    @($node.CatchClauses).Count -gt 0 -and
    $node.CatchClauses[0].Extent.Text.Contains('Restore-TrackerServiceInstall') }, $true))
if ($transaction.Count -ne 1) { throw 'Service start is outside the rollback transaction.' }
if ($syntax.Extent.Text.Contains('Update-TaskTrackerMarketplace.ps1') -or
    $syntax.Extent.Text.Contains('Get-Credential -UserName ([string]$request.targetUser)') -or
    -not $syntax.Extent.Text.Contains('Codex plugins unchanged')) {
    throw 'Installer must leave TargetUser Codex plugins unchanged.'
}
Invoke-Expression $ownerFunction.Extent.Text
function Remember-TrackerAcl([string]$Path) { }
$script:aclResult = $null
function Get-Acl { param([string]$LiteralPath) $acl = [Security.AccessControl.DirectorySecurity]::new();
    $acl.SetOwner([Security.Principal.SecurityIdentifier]'S-1-5-32-544'); return $acl }
function Set-Acl { param([string]$LiteralPath, $AclObject) $script:aclResult = $AclObject }
$newDirectoryLoop = $syntax.Find({ param($node) $node -is [Management.Automation.Language.ForEachStatementAst] -and
    $node.Extent.Text.Contains('Remember-TrackerAcl $dir') -and
    $node.Extent.Text.Contains('$aclBefore[$dir] = Get-Acl') }, $true)
if (-not $newDirectoryLoop) { throw 'New directory ACL is not saved for first-install rollback.' }
function Grant-TrackerRight { param([string]$Path, $Sid, [string]$Rights) }
$aclBefore = @{}
$tasks = Join-Path $base 'new-tasks'
$protected = Join-Path $base 'new-protected'
New-Item -ItemType Directory -Path $protected | Out-Null
$sid = [Security.Principal.SecurityIdentifier]'S-1-5-18'
Invoke-Expression $newDirectoryLoop.Extent.Text
if (-not $aclBefore[$tasks] -or
    $aclBefore[$tasks].GetOwner([Security.Principal.SecurityIdentifier]).Value -cne 'S-1-5-32-544') {
    throw 'First-install tasks ACL was not captured before service owner change.'
}
$catchText = $transaction[0].CatchClauses[0].Extent.Text
if ($catchText.IndexOf('if ($createdAccount)') -lt $catchText.IndexOf('foreach ($path in @($aclBefore.Keys)')) {
    throw 'Service account is deleted before the first-install ACL rollback.'
}
$ownerTest = Join-Path $base 'owner-test'
New-Item -ItemType Directory -Path $ownerTest | Out-Null
$serviceSid = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$targetSid = [Security.Principal.SecurityIdentifier]'S-1-5-32-545'
Set-TrackerDirectoryAcl -Path $ownerTest -ServiceSid $serviceSid -TargetSid $targetSid -IsRoot $false
if ($script:aclResult.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $serviceSid.Value) {
    throw 'First-install tasks root was not assigned to the service SID.'
}
Set-TrackerDirectoryAcl -Path $ownerTest -ServiceSid $serviceSid -TargetSid $targetSid -IsRoot $true
if ($script:aclResult.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne 'S-1-5-32-544') {
    throw 'Tracker root owner was changed.'
}

function Get-Service { param([string]$Name) if ($script:mockService) {
    [pscustomobject]@{ Status = $script:mockStatus } } }
function Stop-Service { param([string]$Name) $script:mockStatus = 'Stopped' }
function Start-Service { param([string]$Name) if ($script:failStart) { throw 'injected SCM failure' }; $script:mockStatus = 'Running' }
function sc.exe { param([string]$Action, [string]$Name) if ($Action -ne 'delete') { throw 'Unexpected SCM action' };
    $script:mockService = $false; $global:LASTEXITCODE = 0 }
$rollback = New-Fixture 'rollback-service' $true
foreach ($name in @('bin', 'runtime')) {
    Move-Item -LiteralPath (Join-Path $rollback.Protected $name) -Destination (Join-Path $rollback.Backup $name)
    New-Item -ItemType Directory -Path (Join-Path $rollback.Protected $name) | Out-Null
}
$script:mockService = $true; $script:mockStatus = 'Running'; $script:failStart = $false
Restore-TrackerServiceInstall -ServiceName 'MockTracker' -ProtectedRoot $rollback.Protected `
    -BackupRoot $rollback.Backup -HadLive @{ bin = $true; runtime = $true } -OldConfig 'old' `
    -ServiceExisted $true -WasRunning $true
Assert-Old $rollback
if ($script:mockStatus -ne 'Running') { throw 'Old service was not restarted after rollback.' }
$failedStart = New-Fixture 'rollback-start-failure' $true
foreach ($name in @('bin', 'runtime')) {
    Move-Item -LiteralPath (Join-Path $failedStart.Protected $name) -Destination (Join-Path $failedStart.Backup $name)
    New-Item -ItemType Directory -Path (Join-Path $failedStart.Protected $name) | Out-Null
}
$script:mockStatus = 'Running'; $script:failStart = $true
$rejected = $false
try {
    Restore-TrackerServiceInstall -ServiceName 'MockTracker' -ProtectedRoot $failedStart.Protected `
        -BackupRoot $failedStart.Backup -HadLive @{ bin = $true; runtime = $true } -OldConfig 'old' `
        -ServiceExisted $true -WasRunning $true
} catch { $rejected = $_.Exception.Message -eq 'injected SCM failure' }
if (-not $rejected) { throw 'SCM start fault was not propagated.' }
Assert-Old $failedStart
$script:failStart = $false
$script:mockService = $true; $script:mockStatus = 'Running'
Restore-TrackerServiceInstall -ServiceName 'MockTracker' -ProtectedRoot $first.Protected `
    -BackupRoot $first.Backup -HadLive @{ bin = $false; runtime = $false } -OldConfig $null `
    -ServiceExisted $false -WasRunning $false
if ($script:mockService) { throw 'New service registration survived first-install rollback.' }

$firstTask = Join-Path $base 'first-task'
$firstTasks = Join-Path $firstTask 'tasks'
$firstProtected = Join-Path $firstTask '.protected'
$snapshotRoot = Join-Path $base 'snapshot-root'
New-Item -ItemType Directory -Path $firstTasks, $firstProtected, $snapshotRoot -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $firstTask 'projects.json'),
    '{"schema_version":1,"projects":[{"project_key":"JIRA","source_type":"JIRA_CLOUD","jira_host":"https://example.test"}]}',
    [Text.UTF8Encoding]::new($false))
$serviceSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$firstConfig = Join-Path $base 'first-task-service.json'
@{ schemaVersion = 2; trackerRoot = $firstTask; tasksRoot = $firstTasks; protectedRoot = $firstProtected
    serviceAccountSid = $serviceSid; agentSid = 'S-1-5-32-545'; pwshPath = (Get-Command pwsh.exe).Source
    snapshotRoots = @($snapshotRoot) } | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $firstConfig -Encoding utf8
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Repository test .venv is missing.' }
$mockHelper = Join-Path $base 'mock-task-acl.ps1'
[IO.File]::WriteAllText($mockHelper, 'param([string]$ConfigPath,[string]$TaskFolder) if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or -not (Test-Path -LiteralPath $TaskFolder -PathType Container)) { throw ''Invalid first task'' }; [IO.File]::WriteAllText((Join-Path $PSScriptRoot ''acl-called.txt''), $TaskFolder)', [Text.UTF8Encoding]::new($false))
$taskCode = 'import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.argv[1]))); from task_folder import FolderStore; store=FolderStore(sys.argv[2]); store.helper=Path(sys.argv[3]); print(store.create_task_folder(''JIRA-1'')[''status''])'
$ErrorActionPreference = 'Continue'
try { $created = & $python -I -c $taskCode (Join-Path $repo 'src') $firstConfig $mockHelper 2>&1 }
finally { $ErrorActionPreference = 'Stop' }
if ($LASTEXITCODE -ne 0 -or $created -cne 'created') { throw "Isolated first create_task_folder failed: $created" }
if (-not (Test-Path -LiteralPath (Join-Path $base 'acl-called.txt') -PathType Leaf)) {
    throw 'First create_task_folder did not invoke the ACL helper.'
}

Write-Output 'Layout, first task, owner, SCM rollback, token-pair and host reuse checks passed'
