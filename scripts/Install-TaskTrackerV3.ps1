param([Parameter(Mandatory)][string]$ConfigPath)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1')
. (Join-Path $PSScriptRoot 'TaskTracker-InstallLayout.ps1')
Assert-TrackerAdministrator
if (-not (Test-AbsoluteWindowsPath $ConfigPath)) { throw 'ConfigPath must be absolute.' }
$request = Read-TrackerUtf8File $ConfigPath | ConvertFrom-Json
$bootstrapOriginal = Read-TrackerUtf8File $ConfigPath
if ($request.schemaVersion -ne 3) { throw 'Administrative bootstrap JSON v3 is required.' }
if ([string]$request.marketplaceSource -cne 'https://github.com/twilight-slider/ai-marketplace.git') {
    throw 'Bootstrap JSON must identify the published ai-marketplace Git source.'
}
$currentServiceSid = Get-InstalledTrackerServiceSid -TargetSid ([string]$request.ownerSid)
Assert-TrackerProtectedFile -Path $ConfigPath -ServiceSid $currentServiceSid | Out-Null
$settings = Get-TargetTrackerSettings -TargetUser ([string]$request.targetUser)
if ($settings.TargetSid -cne [string]$request.ownerSid -or
    $settings.TrackerRoot -ine [string]$request.trackerRoot -or
    $settings.EnvPath -ine [string]$request.targetEnvPath) {
    throw 'TargetUser settings differ from bootstrap JSON.'
}
$repository = Assert-PlainDirectory ([string]$request.repositoryRoot)
Assert-TaskTrackerCheckout -RepositoryRoot $repository -ExpectedCommit ([string]$request.repositoryCommit) | Out-Null
$installedSid = Get-InstalledTrackerServiceSid -TargetSid $settings.TargetSid
Assert-TrackerRootBoundary -TrackerRoot $settings.TrackerRoot -TargetSid $settings.TargetSid -ServiceSid $installedSid `
    -TrustAuthenticatedUsers ([bool]$request.trustAuthenticatedUsers) | Out-Null
$protected = Assert-TrackerProtectedArea -TrackerRoot $settings.TrackerRoot -TargetSid $settings.TargetSid -ServiceSid $installedSid
if ([IO.Path]::GetFullPath($protected) -ine [IO.Path]::GetFullPath([string]$request.protectedRoot) -or
    [IO.Path]::GetFullPath($ConfigPath) -ine [IO.Path]::GetFullPath((Join-Path $protected 'bootstrap.json'))) {
    throw 'Protected paths differ from bootstrap JSON.'
}
$serviceName = [string]$request.serviceName
if ($serviceName -cne "TaskTracker-$($settings.TargetSid.Split('-')[-1])" -or
    [string]$request.pipeName -cne "task-tracker-$($settings.TargetSid.Split('-')[-1])-v1" -or
    [string]$request.installRoot -ine (Join-Path $protected 'bin') -or
    [string]$request.runtimeRoot -ine (Join-Path $protected 'runtime') -or
    [int]$request.mcpPort -lt 1 -or [int]$request.mcpPort -gt 65535) {
    throw 'Bootstrap service fields are inconsistent.'
}
$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($service -and $installedSid -cne [string]$request.serviceAccountSid) {
    throw 'Installed service account SID differs from bootstrap JSON.'
}
$account = Get-LocalUser -Name ([string]$request.serviceAccountName) -ErrorAction SilentlyContinue
if ($account -and [string]$request.serviceAccountSid -and $account.SID.Value -cne [string]$request.serviceAccountSid) {
    throw 'Service account SID differs from bootstrap JSON.'
}
if ($account) {
    if ($account.SID.Value -eq $settings.TargetSid) { throw 'Service account must differ from TargetUser.' }
    $administrators = Get-LocalGroup -SID 'S-1-5-32-544'
    if (Get-LocalGroupMember -Group $administrators.Name -ErrorAction SilentlyContinue |
        Where-Object { $_.SID.Value -eq $account.SID.Value }) {
        throw 'Service account must not be an administrator.'
    }
}
$exe = Join-Path $protected 'bin\TaskTrackerService.exe'
$serviceConfig = Join-Path $protected 'service.json'
foreach ($name in @('bin', 'runtime', 'state', 'logs')) {
    $path = Join-Path $protected $name
    if (Test-Path -LiteralPath $path) { Assert-PlainDirectory $path | Out-Null }
}
foreach ($path in @((Join-Path $settings.TrackerRoot 'projects.json'), $exe, $serviceConfig)) {
    if (Test-Path -LiteralPath $path) {
        $file = Get-Item -LiteralPath $path -Force
        if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Installed file must be plain: $path"
        }
    }
}
if ($service) {
    $scm = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
    $accountName = ([string]$scm.StartName) -replace '^\.(?=\\)', [Environment]::MachineName
    if ([string]$scm.PathName -ine "`"$exe`" --config `"$serviceConfig`"" -or
        ([Security.Principal.NTAccount]$accountName).Translate([Security.Principal.SecurityIdentifier]).Value -cne $installedSid) {
        throw 'Existing service binding differs from expected executable/config/account.'
    }
    if (-not (Test-Path -LiteralPath $serviceConfig -PathType Leaf)) { throw 'Existing service.json is missing.' }
    Assert-TrackerProtectedFile -Path $serviceConfig -ServiceSid $installedSid | Out-Null
}
$prior = if (Test-Path -LiteralPath $serviceConfig -PathType Leaf) {
    Read-TrackerUtf8File $serviceConfig | ConvertFrom-Json
} else { $null }
if ($prior -and [string]$prior.trackerRoot -ine $settings.TrackerRoot) {
    throw 'Existing service is bound to another Tracker root.'
}
$snapshotRoots = @($request.snapshotRoots | Where-Object { $_ })
if ($snapshotRoots.Count -eq 0) { throw 'Bootstrap JSON must specify at least one snapshot root.' }
$snapshotRoots = @(Assert-TrackerSnapshotRoots -Roots $snapshotRoots -TrackerRoot $settings.TrackerRoot)
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$pwshFile = Get-Item -LiteralPath $pwsh -Force
if ($pwshFile.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Executable is a reparse point: $pwsh" }
$stage = Join-Path $protected ('.stage-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $protected ('.rollback-' + [guid]::NewGuid().ToString('N'))
$stageBin = Join-Path $stage 'bin'
$stageRuntime = Join-Path $stage 'runtime'
$hostHash = (Get-FileHash -LiteralPath (Join-Path $repository 'src\ServiceHost.cs') -Algorithm SHA256).Hash
$reuseExe = Test-TrackerHostReuse -PriorConfig $prior -SourceHash $hostHash -ExistingExe $exe
if (-not $reuseExe) {
    $windows = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
    $csc = Join-Path $windows 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $csc -PathType Leaf)) { throw 'C# compiler is missing.' }
    $cscFile = Get-Item -LiteralPath $csc -Force
    if ($cscFile.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Executable is a reparse point: $csc" }
}
$createdAccount = $false
$createdService = $false
$bootstrapUpdated = $false
$wasRunning = $service -and $service.Status -eq 'Running'
$switched = $false
$preserveBackup = $false
$hadLive = @{ bin = (Test-Path -LiteralPath (Join-Path $protected 'bin')); runtime = (Test-Path -LiteralPath (Join-Path $protected 'runtime')) }
$oldConfig = if ($prior) { Read-TrackerUtf8File $serviceConfig } else { $null }
$protectedTokenPath = Join-Path $protected 'mcp-token'
$tokenRollback = Get-TrackerTokenRollbackState -ProtectedTokenPath $protectedTokenPath -EnvPath $settings.EnvPath
$tokenSyncAttempted = $false
$aclBefore = [ordered]@{}
function Remember-TrackerAcl([string]$Path) {
    if (-not $aclBefore.Contains($Path)) {
        $aclBefore[$Path] = if (Test-Path -LiteralPath $Path) { Get-Acl -LiteralPath $Path } else { $null }
    }
}
function Grant-TrackerRight([string]$Path, [Security.Principal.SecurityIdentifier]$Sid, [string]$Rights) {
    Remember-TrackerAcl $Path
    $acl = Get-Acl -LiteralPath $Path
    $inherit = if ((Get-Item -LiteralPath $Path).PSIsContainer) { 'ContainerInherit, ObjectInherit' } else { 'None' }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($Sid, $Rights, $inherit, 'None', 'Allow'))
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Set-TrackerDirectoryAcl([string]$Path, [Security.Principal.SecurityIdentifier]$ServiceSid,
    [Security.Principal.SecurityIdentifier]$TargetSid, [bool]$IsRoot) {
    Remember-TrackerAcl $Path
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    if (-not $IsRoot) { $acl.SetOwner($ServiceSid) }
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544', $ServiceSid.Value)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]$sidValue, 'FullControl',
            'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    if ($IsRoot) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($TargetSid, 'ReadAndExecute', 'None', 'None', 'Allow'))
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($TargetSid, 'ReadAndExecute', 'ContainerInherit', 'InheritOnly', 'Allow'))
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($TargetSid, 'Read', 'ObjectInherit', 'InheritOnly', 'Allow'))
    } else {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($TargetSid, 'ReadAndExecute', 'None', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Protect-TrackerManifest([string]$Path, [Security.Principal.SecurityIdentifier]$ServiceSid) {
    Remember-TrackerAcl $Path
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544', $ServiceSid.Value)) {
        $sidValue = [Security.Principal.SecurityIdentifier]$sidValue
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sidValue, 'FullControl', 'Allow'))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]$settings.TargetSid, 'Read', 'Allow'))
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Assert-InstalledMcp([int]$Port, [string]$Token) {
    $uri = "http://127.0.0.1:$Port/mcp/folders"
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        try {
            $r = Invoke-RestMethod -Uri $uri -Method Post -ContentType 'application/json' `
                -Headers @{ Authorization = "Bearer $Token" } -Body '{"jsonrpc":"2.0","id":1,"method":"ping"}' -TimeoutSec 3
            if ($r.jsonrpc -ceq '2.0' -and $null -ne $r.result) { return }
        } catch { }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Installed MCP did not accept the configured token.'
}
try {
    New-Item -ItemType Directory -Path $stageBin, $backup -Force | Out-Null
    & (Join-Path $repository 'scripts\Stage-PythonRuntime.ps1') -RuntimeRoot $stageRuntime | Out-Null
    foreach ($name in @('folder_worker.py', 'task_folder.py', 'result_snapshot_store.py', 'Set-TaskDirectoryAcl.ps1')) {
        Copy-Item -LiteralPath (Join-Path $repository "src\$name") -Destination $stageBin
    }
    if ($reuseExe) {
        Copy-Item -LiteralPath $exe -Destination (Join-Path $stageBin 'TaskTrackerService.exe')
        if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -cne
            (Get-FileHash -LiteralPath (Join-Path $stageBin 'TaskTrackerService.exe') -Algorithm SHA256).Hash) {
            throw 'Stable service EXE copy hash mismatch.'
        }
    } else {
        & $csc /nologo /target:exe "/out:$(Join-Path $stageBin 'TaskTrackerService.exe')" `
            /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll `
            (Join-Path $repository 'src\ServiceHost.cs')
        if ($LASTEXITCODE -ne 0) { throw 'Staged ServiceHost compilation failed.' }
    }
    $python = Join-Path $stageRuntime 'Scripts\python.exe'
    $probeScript = Join-Path $stageBin '.probe.py'
    [IO.File]::WriteAllText($probeScript,
        "import sys`nfrom pathlib import Path`nsys.dont_write_bytecode = True`nsys.path.insert(0, str(Path(__file__).parent))`nimport folder_worker, task_folder, result_snapshot_store, yaml`n",
        [Text.UTF8Encoding]::new($false))
    try {
        $probe = & $python -I $probeScript 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Staged Python worker import failed: $probe" }
    } finally { Remove-Item -LiteralPath $probeScript -ErrorAction SilentlyContinue }

    $tokenSyncAttempted = $true
    & (Join-Path $repository 'scripts\Sync-TaskTrackerToken.ps1') -ConfigPath $ConfigPath | Out-Null
    $token = (Read-TrackerUtf8File (Join-Path $protected 'mcp-token')).Trim()
    if (-not $token) { throw 'MCP token is empty.' }

    if (-not $account) {
        $password = Read-Host "Password for new service account $($request.serviceAccountName)" -AsSecureString
        $account = New-LocalUser -Name ([string]$request.serviceAccountName) -Password $password -Description 'Personal Task Tracker service'
        $createdAccount = $true
    } elseif (-not $service) {
        $password = Read-Host "Password for existing service account $($request.serviceAccountName)" -AsSecureString
    }
    if ($account.SID.Value -eq $settings.TargetSid) { throw 'Service account must differ from TargetUser.' }
    Add-Type -Path (Join-Path $repository 'scripts\ServiceLogonRight.cs')
    $sidBytes = [byte[]]::new($account.SID.BinaryLength)
    $account.SID.GetBinaryForm($sidBytes, 0)
    [TaskFolderServiceLogonRight]::Grant($sidBytes) | Out-Null
    $sid = [Security.Principal.SecurityIdentifier]$account.SID.Value
    Set-TrackerDirectoryAcl -Path $settings.TrackerRoot -ServiceSid $sid `
        -TargetSid ([Security.Principal.SecurityIdentifier]$settings.TargetSid) -IsRoot $true
    Grant-TrackerRight -Path $protected -Sid $sid -Rights 'FullControl'
    $tasks = Join-Path $settings.TrackerRoot 'tasks'
    foreach ($dir in @($tasks, (Join-Path $protected 'state'), (Join-Path $protected 'logs'))) {
        Remember-TrackerAcl $dir
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir | Out-Null
            $aclBefore[$dir] = Get-Acl -LiteralPath $dir
        }
        Assert-PlainDirectory $dir | Out-Null
        Grant-TrackerRight -Path $dir -Sid $sid -Rights 'FullControl'
    }
    Set-TrackerDirectoryAcl -Path $tasks -ServiceSid $sid `
        -TargetSid ([Security.Principal.SecurityIdentifier]$settings.TargetSid) -IsRoot $false
    $projects = Join-Path $settings.TrackerRoot 'projects.json'
    Remember-TrackerAcl $projects
    if (-not (Test-Path -LiteralPath $projects)) {
        [IO.File]::WriteAllText($projects, "{`"schema_version`":1,`"projects`":[]}`n", [Text.UTF8Encoding]::new($false))
    }
    Protect-TrackerManifest -Path $projects -ServiceSid $sid
    $config = [ordered]@{
        schemaVersion = 2; trackerRoot = $settings.TrackerRoot; tasksRoot = $tasks
        protectedRoot = $protected; installRoot = (Join-Path $protected 'bin')
        serviceName = $serviceName; serviceAccountSid = $account.SID.Value
        agentSid = $settings.TargetSid; pipeName = [string]$request.pipeName
        workerLanguage = 'python'; mcpPort = [int]$request.mcpPort
        pwshPath = $pwsh; snapshotRoots = $snapshotRoots; hostSourceSha256 = $hostHash
    }
    $newConfig = ($config | ConvertTo-Json -Depth 5) + "`n"
    [IO.File]::WriteAllText((Join-Path $stage 'service.json'), $newConfig, [Text.UTF8Encoding]::new($false))

    $switched = $true
    if ($wasRunning) { Stop-Service -Name $serviceName -ErrorAction Stop }
    foreach ($name in @('bin', 'runtime')) {
        $live = Join-Path $protected $name
        if (Test-Path -LiteralPath $live) { Move-Item -LiteralPath $live -Destination (Join-Path $backup $name) }
        Move-Item -LiteralPath (Join-Path $stage $name) -Destination $live
    }
    if ($oldConfig) { [IO.File]::WriteAllText((Join-Path $backup 'service.json'), $oldConfig, [Text.UTF8Encoding]::new($false)) }
    Move-Item -LiteralPath (Join-Path $stage 'service.json') -Destination $serviceConfig -Force
    if (-not $service) {
        $credential = [pscredential]::new(".\$($request.serviceAccountName)", $password)
        New-Service -Name $serviceName -BinaryPathName "`"$exe`" --config `"$serviceConfig`"" `
            -Credential $credential -StartupType Automatic -Description 'Personal Task Tracker service' | Out-Null
        $createdService = $true
    } else { Set-Service -Name $serviceName -StartupType Automatic -ErrorAction Stop }
    Start-Service -Name $serviceName -ErrorAction Stop
    $running = Get-Service -Name $serviceName -ErrorAction Stop
    if ($running.Status -ne 'Running' -or $running.StartType -ne 'Automatic') { throw 'Service did not reach Running/Automatic.' }
    Assert-InstalledMcp -Port ([int]$request.mcpPort) -Token $token
    if (-not [string]$request.serviceAccountSid) {
        $request.serviceAccountSid = $account.SID.Value
        $tempBootstrap = Join-Path $protected ('.bootstrap-sid-' + [guid]::NewGuid().ToString('N') + '.json')
        try {
            [IO.File]::WriteAllText($tempBootstrap, (($request | ConvertTo-Json -Depth 6) + "`n"), [Text.UTF8Encoding]::new($false))
            $bootstrapUpdated = $true
            Move-Item -LiteralPath $tempBootstrap -Destination $ConfigPath -Force
        } finally { if (Test-Path -LiteralPath $tempBootstrap) { Remove-Item -LiteralPath $tempBootstrap } }
    }
    $instructions = Join-Path $tasks 'AGENTS.md'
    if (-not (Test-Path -LiteralPath $instructions)) {
        $source = Join-Path $settings.TrackerRoot 'AGENTS.md'
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            $source = Join-Path $repository 'templates\AGENTS.md'
        }
        Copy-Item -LiteralPath $source -Destination $instructions
    }
    Protect-TrackerManifest -Path $instructions -ServiceSid $sid
    foreach ($name in @('AGENTS.md', 'AGENTS.before-AIDEV-60.md', 'mcp-adapter.js', 'tracker-client.json')) {
        $legacy = Join-Path $settings.TrackerRoot $name
        if (Test-Path -LiteralPath $legacy) {
            $item = Get-Item -LiteralPath $legacy -Force
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Legacy root entry is not a plain file: $legacy"
            }
            Remove-Item -LiteralPath $legacy
        }
    }
    $unexpected = @(Get-ChildItem -LiteralPath $settings.TrackerRoot -File -Force |
        Where-Object Name -ne 'projects.json' | Select-Object -ExpandProperty Name)
    if ($unexpected.Count) { throw "Unexpected Tracker root files remain: $($unexpected -join ', ')" }
    $preserveBackup = $true
} catch {
    $failure = $_.Exception.Message
    $tokenRollbackFailure = $null
    if ($tokenSyncAttempted) {
        try {
            Restore-TrackerTokenPair -State $tokenRollback
        } catch {
            $preserveBackup = $true
            $tokenRollbackFailure = $_.Exception.Message
        }
    }
    if ($switched) {
        try {
            Restore-TrackerServiceInstall -ServiceName $serviceName -ProtectedRoot $protected -BackupRoot $backup `
                -HadLive $hadLive -OldConfig $oldConfig -ServiceExisted ([bool]$service) -WasRunning ([bool]$wasRunning)
        } catch {
            $preserveBackup = $true
            throw "RECOVERY_INCOMPLETE: installation failed ($failure); rollback failed: $($_.Exception.Message)"
        }
    }
    if ($bootstrapUpdated) {
        try { [IO.File]::WriteAllText($ConfigPath, $bootstrapOriginal, [Text.UTF8Encoding]::new($false)) }
        catch {
            $preserveBackup = $true
            throw "RECOVERY_INCOMPLETE: installation failed ($failure); bootstrap JSON rollback failed"
        }
    }
    foreach ($path in @($aclBefore.Keys) | Sort-Object Length -Descending) {
        if ($aclBefore[$path] -and (Test-Path -LiteralPath $path)) {
            try { Set-Acl -LiteralPath $path -AclObject $aclBefore[$path] }
            catch {
                $preserveBackup = $true
                throw "RECOVERY_INCOMPLETE: installation failed ($failure); ACL rollback failed at $path"
            }
        }
    }
    if ($createdAccount) {
        try { Remove-LocalUser -Name ([string]$request.serviceAccountName) -ErrorAction Stop }
        catch {
            $preserveBackup = $true
            throw "RECOVERY_INCOMPLETE: installation failed ($failure); newly created service account remains: $($_.Exception.Message)"
        }
    }
    if ($tokenRollbackFailure) {
        throw "RECOVERY_INCOMPLETE: installation failed ($failure); previous service restored where present, but MCP token rollback failed: $tokenRollbackFailure"
    }
    throw "Installation failed; previous service restored where present: $failure"
} finally {
    Remove-TrackerInstallTree -ProtectedRoot $protected -Path $stage
    if (-not $preserveBackup) { Remove-TrackerInstallTree -ProtectedRoot $protected -Path $backup }
}
Write-Output "Installed: $serviceName; EXE reused: $reuseExe; Python runtime: $($request.runtimeRoot); Codex plugins unchanged; rollback retained: $backup"
Write-Output "For $($request.targetUser): verify task-folder-workflow@ai-marketplace 0.6.1 and task-tracker-mcp@ai-marketplace 0.2.1; if already enabled, no action is needed."
Write-Output "If ai-marketplace is missing: codex plugin marketplace add $($request.marketplaceSource)"
Write-Output 'If versions are outdated: codex plugin marketplace upgrade ai-marketplace'
Write-Output 'If missing: codex plugin add task-folder-workflow@ai-marketplace'
Write-Output 'If missing: codex plugin add task-tracker-mcp@ai-marketplace'
