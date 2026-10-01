param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [switch]$ValidateOnly,
    [switch]$MigrateTasks
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $ValidateOnly -and -not $elevated) { throw 'Installation requires an elevated PowerShell window.' }
if (-not [IO.Path]::IsPathFullyQualified($ConfigPath)) { throw 'ConfigPath must be absolute.' }
$requestFile = Get-Item -LiteralPath $ConfigPath -Force -ErrorAction Stop
if ($requestFile.PSIsContainer -or ($requestFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Bootstrap request must be a regular file.' }
$request = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$issues = [Collections.Generic.List[object]]::new()
function Add-Issue([string]$code, [string]$path, [string]$actual, [string]$required, [string]$remedy) {
    $script:issues.Add([pscustomobject]@{ Code = $code; Path = $path; Actual = $actual; Required = $required; Remedy = $remedy })
}
function Stop-OnIssues {
    if (-not $script:issues.Count) { return }
    $lines = @($script:issues | ForEach-Object { "[$($_.Code)] $($_.Path); фактически: $($_.Actual); требуется: $($_.Required); исправить: $($_.Remedy)" })
    throw "PRECHECK FAILED ($($script:issues.Count)):`n$($lines -join "`n")"
}
$root = [string]$request.trackerRoot
if (-not [IO.Path]::IsPathFullyQualified($root)) { throw 'Tracker root must be absolute.' }
$root = [IO.Path]::GetFullPath($root).TrimEnd('\')
$volume = [IO.Path]::GetPathRoot($root)
if ($root -eq $volume.TrimEnd('\')) { throw 'Tracker root cannot be a volume root.' }
$rid = ([string]$request.ownerSid).Split('-')[-1]
$serviceName = "TaskTracker-$rid"
foreach ($field in @(
    @{ Name = 'schemaVersion'; Actual = $request.schemaVersion; Required = 2 },
    @{ Name = 'agentSid'; Actual = $request.agentSid; Required = $request.ownerSid },
    @{ Name = 'trackerRoot'; Actual = $request.trackerRoot; Required = $root },
    @{ Name = 'tasksRoot'; Actual = $request.tasksRoot; Required = (Join-Path $root 'tasks') },
    @{ Name = 'protectedRoot'; Actual = $request.protectedRoot; Required = (Join-Path $root '.protected') },
    @{ Name = 'installRoot'; Actual = $request.installRoot; Required = (Join-Path $root '.protected\bin') },
    @{ Name = 'serviceName'; Actual = $request.serviceName; Required = $serviceName },
    @{ Name = 'pipeName'; Actual = $request.pipeName; Required = "task-tracker-$rid-v1" }
)) {
    if ($field.Actual -cne $field.Required) {
        Add-Issue 'REQUEST_FIELD' "${ConfigPath}::$($field.Name)" ([string]$field.Actual) ([string]$field.Required) 'Run bootstrap again under the target user; do not edit the JSON manually.'
    }
}
if ($request.ownerSid -notmatch '^S-1-5-21-(?:[0-9]+-){3}[0-9]+$') { Add-Issue 'REQUEST_OWNER_SID' "${ConfigPath}::ownerSid" ([string]$request.ownerSid) 'Valid target-user SID' 'Run bootstrap under the target user.' }
if ($request.serviceAccountName -notmatch '^[\p{L}\p{N}._-]{1,20}$') { Add-Issue 'REQUEST_ACCOUNT_NAME' "${ConfigPath}::serviceAccountName" ([string]$request.serviceAccountName) 'Windows account name, at most 20 characters' 'Run bootstrap with a valid -ServiceAccountName.' }
if (-not [IO.Path]::IsPathFullyQualified([string]$request.nodePath)) { Add-Issue 'REQUEST_NODE_PATH' "${ConfigPath}::nodePath" ([string]$request.nodePath) 'Absolute Node.js path' 'Install Node.js in a protected directory and rerun bootstrap.' }
try { $requestOwner = (Get-Acl -LiteralPath $ConfigPath).GetOwner([Security.Principal.SecurityIdentifier]).Value }
catch { Add-Issue 'REQUEST_ACL_UNREADABLE' $ConfigPath $_.Exception.Message 'Readable JSON ACL' 'Fix access to the bootstrap JSON.'; $requestOwner = $null }
if ($requestOwner -and $requestOwner -ne $request.ownerSid) { Add-Issue 'REQUEST_FILE_OWNER' $ConfigPath $requestOwner ([string]$request.ownerSid) 'Run bootstrap under the target user.' }
$account = if ($request.serviceAccountName -match '^[\p{L}\p{N}._-]{1,20}$') {
    Get-LocalUser -Name $request.serviceAccountName -ErrorAction SilentlyContinue
} else { $null }
if ($request.serviceAccountSid -and (-not $account -or $request.serviceAccountSid -ne $account.SID.Value)) {
    Add-Issue 'SERVICE_ACCOUNT_SID' ([string]$request.serviceAccountName) $(if ($account) { $account.SID.Value } else { 'Account missing' }) ([string]$request.serviceAccountSid) 'A changed service account requires a full reinstall.'
}
if ($account) {
    if ($account.SID.Value -eq $request.agentSid) { Add-Issue 'SERVICE_ACCOUNT_AGENT' ([string]$request.serviceAccountName) 'Same SID as target user' 'Dedicated standard service account' 'Choose a separate -ServiceAccountName and rerun bootstrap.' }
    $adminGroup = Get-LocalGroup -SID 'S-1-5-32-544'
    if (Get-LocalGroupMember -Group $adminGroup.Name -ErrorAction SilentlyContinue | Where-Object { $_.SID.Value -eq $account.SID.Value }) {
        Add-Issue 'SERVICE_ACCOUNT_ADMIN' ([string]$request.serviceAccountName) 'Member of Administrators' 'Dedicated standard account' 'Choose a non-administrator -ServiceAccountName and rerun bootstrap.'
    }
}
$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
$installedConfig = Join-Path $root '.protected\service.json'
$exe = Join-Path $root '.protected\bin\TaskTrackerService.exe'

# Only principals with administrative control may own or alter a path used by the service.
$trustedSids = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
# S-1-5-32 is the BUILTIN account domain, not a user or group in an access token.
$danger = [Security.AccessControl.FileSystemRights]'ChangePermissions, TakeOwnership, DeleteSubdirectoriesAndFiles'
function Get-ParentRemedy([string]$path) {
    if ($path -eq $volume) { return 'Choose a dedicated Tracker location below the volume root; do not change the volume ACL.' }
    $shared = @([IO.Path]::GetDirectoryName([Environment]::GetFolderPath('UserProfile')),
        [Environment]::GetFolderPath('UserProfile'),
        [Environment]::GetFolderPath('Windows'), [Environment]::GetFolderPath('ProgramFiles'),
        [Environment]::GetFolderPath('CommonApplicationData')) | Where-Object { $_ }
    foreach ($base in $shared) {
        $base = [IO.Path]::GetFullPath($base).TrimEnd('\')
        if ($path -eq $base -or $path.StartsWith("$base\", [StringComparison]::OrdinalIgnoreCase)) {
            return 'Choose a dedicated Tracker location; do not change this shared/system/user directory automatically.'
        }
    }
    $relative = [IO.Path]::GetRelativePath($path, $root)
    $child = Join-Path $path ($relative.Split('\')[0])
    $scriptPath = Join-Path $PSScriptRoot 'Protect-TrackerParent.ps1'
    $quotedScript = $scriptPath.Replace("'", "''")
    $quotedChild = $child.Replace("'", "''")
    $command = "pwsh -NoProfile -File '$quotedScript' -TrackerRoot '$quotedChild'"
    return "Only if this parent is dedicated: preview: $command; after READY, apply: $command -Apply"
}
function Test-AgentCannotAlter([string]$path, [bool]$volumeRoot) {
    try { $acl = Get-Acl -LiteralPath $path }
    catch { Add-Issue 'PARENT_ACL_UNREADABLE' $path $_.Exception.Message 'Readable ACL' 'Fix access to this directory and retry.'; return }
    foreach ($rule in $acl.Access) {
        $mask = ([long][int]$rule.FileSystemRights) -band [long]4294967295
        if ($rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly -or
            $rule.AccessControlType -ne 'Allow' -or
            (-not ($mask -band $danger) -and -not ($mask -band [long]268435456) -and ($volumeRoot -or
            -not ($mask -band [Security.AccessControl.FileSystemRights]::Delete)))) { continue }
        $sid = try { $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
            catch { Add-Issue 'PARENT_SID_UNRESOLVED' $path ([string]$rule.IdentityReference) 'Resolvable ACL principal' (Get-ParentRemedy $path); continue }
        if ($sid -notin $trustedSids -and $sid -ne 'S-1-5-32') {
            Add-Issue 'PARENT_UNSAFE_GRANT' $path "$sid has $($rule.FileSystemRights)" 'No untrusted effective Delete, DeleteSubdirectoriesAndFiles, ChangePermissions or TakeOwnership' (Get-ParentRemedy $path)
        }
    }
}
function Test-TrustedExecutable([string]$path) {
    if (-not [IO.Path]::IsPathFullyQualified($path)) { Add-Issue 'EXECUTABLE_RELATIVE' $path 'Relative path' 'Absolute path' 'Install the executable in a protected directory and rerun bootstrap.'; return }
    $path = [IO.Path]::GetFullPath($path)
    try { $file = Get-Item -LiteralPath $path -Force -ErrorAction Stop }
    catch { Add-Issue 'EXECUTABLE_MISSING' $path $_.Exception.Message 'Existing regular executable' 'Install the executable in a protected directory.'; return }
    if ($file.PSIsContainer) { Add-Issue 'EXECUTABLE_NOT_FILE' $path 'Directory' 'Regular file' 'Select a protected executable file.' }
    $cursor = $path
    $write = [Security.AccessControl.FileSystemRights]'WriteData, AppendData, WriteAttributes, WriteExtendedAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    $rootWrite = [Security.AccessControl.FileSystemRights]'DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    while ($cursor) {
        try { $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop; $acl = Get-Acl -LiteralPath $cursor }
        catch { Add-Issue 'EXECUTABLE_ACL_UNREADABLE' $cursor $_.Exception.Message 'Readable file and ACL' 'Fix access or install the executable in a protected directory.'; break }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Add-Issue 'EXECUTABLE_REPARSE' $cursor 'Reparse point' 'Plain file or directory' 'Install the executable in a protected directory without reparse points.' }
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trustedSids) {
            Add-Issue 'EXECUTABLE_OWNER' $cursor ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value) 'SYSTEM, Administrators or TrustedInstaller owner' 'Install the executable in a protected administrator-owned directory.'
        }
        $mask = if ($cursor -eq [IO.Path]::GetPathRoot($cursor)) { $rootWrite } else { $write }
        foreach ($rule in $acl.Access) {
            $rights = ([long][int]$rule.FileSystemRights) -band [long]4294967295
            if ($rule.AccessControlType -ne 'Allow' -or
                $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly -or
                (-not ($rights -band $mask) -and -not ($rights -band [long]268435456) -and
                ($cursor -eq [IO.Path]::GetPathRoot($cursor) -or -not ($rights -band [long]1073741824)))) { continue }
            $sid = try { $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
                catch { Add-Issue 'EXECUTABLE_SID_UNRESOLVED' $cursor ([string]$rule.IdentityReference) 'Resolvable ACL principal' 'Fix the executable ACL.'; continue }
            if ($sid -notin $trustedSids -and $sid -ne 'S-1-5-32') { Add-Issue 'EXECUTABLE_UNSAFE_GRANT' $cursor "$sid has $($rule.FileSystemRights)" 'No untrusted effective write/delete/ACL rights' 'Install the executable in a protected administrator-owned directory.' }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}
$nodePath = if ([IO.Path]::IsPathFullyQualified([string]$request.nodePath)) { [IO.Path]::GetFullPath([string]$request.nodePath) } else { $null }
$pwshPath = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $pwshPath) { Add-Issue 'PWSH_MISSING' 'pwsh.exe' 'Not found on PATH' 'Protected PowerShell 7 executable' 'Install PowerShell 7 in a protected directory.' }
$windows = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
$csc = Join-Path $windows 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
foreach ($executable in @($nodePath, $pwshPath, $csc) | Where-Object { $_ }) { Test-TrustedExecutable $executable }
if ($service) {
    try { $actual = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop }
    catch { Add-Issue 'SERVICE_UNREADABLE' $serviceName $_.Exception.Message 'Readable service configuration' 'Check the service and retry.'; $actual = $null }
    if ($actual -and ($actual.PathName -ne "`"$exe`" --config `"$installedConfig`"" -or
        $actual.StartName -notmatch ('(?i)(^|\\)' + [regex]::Escape($request.serviceAccountName) + '$'))) {
        Add-Issue 'SERVICE_BINDING' $serviceName "Path: $($actual.PathName); account: $($actual.StartName)" "Executable: $exe; account: $($request.serviceAccountName)" 'A different service binding requires a full reinstall.'
    }
    if (-not (Test-Path -LiteralPath $installedConfig)) {
        Add-Issue 'SERVICE_CONFIG_MISSING' $installedConfig 'Missing' 'Installed service config' 'Repair the service by full reinstall.'
    } else {
        try { $pinned = Get-Content -LiteralPath $installedConfig -Raw | ConvertFrom-Json }
        catch { Add-Issue 'SERVICE_CONFIG_UNREADABLE' $installedConfig $_.Exception.Message 'Readable service config' 'Repair the service by full reinstall.'; $pinned = $null }
        if ($pinned -and $pinned.trackerRoot -ne $root) { Add-Issue 'SERVICE_ROOT' $installedConfig ([string]$pinned.trackerRoot) $root 'Moving Tracker requires a full reinstall.' }
    }
}
$cursor = [IO.Path]::GetDirectoryName($root)
while ($cursor) {
    if (Test-Path -LiteralPath $cursor) {
        try { $item = Get-Item -LiteralPath $cursor -Force; $acl = Get-Acl -LiteralPath $cursor }
        catch { Add-Issue 'PARENT_UNREADABLE' $cursor $_.Exception.Message 'Plain directory with readable ACL' (Get-ParentRemedy $cursor); $item = $null; $acl = $null }
        if ($item -and (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) { Add-Issue 'PARENT_NOT_PLAIN' $cursor 'File or reparse point' 'Plain directory' 'Choose a plain dedicated Tracker path.' }
        if ($acl) {
            $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            if ($cursor -ne $volume -and $owner -notin $trustedSids) { Add-Issue 'PARENT_OWNER' $cursor $owner 'SYSTEM, Administrators or TrustedInstaller owner' (Get-ParentRemedy $cursor) }
            Test-AgentCannotAlter $cursor ($cursor -eq $volume)
        }
    }
    if ($cursor -eq $volume) { break }
    $cursor = [IO.Path]::GetDirectoryName($cursor)
}
if (Test-Path -LiteralPath $root) {
    try { $item = Get-Item -LiteralPath $root -Force; $acl = Get-Acl -LiteralPath $root }
    catch { Add-Issue 'TRACKER_ROOT_UNREADABLE' $root $_.Exception.Message 'Plain directory with readable ACL' 'Choose a plain dedicated Tracker path.'; $item = $null; $acl = $null }
    if ($item -and (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) { Add-Issue 'TRACKER_ROOT_NOT_PLAIN' $root 'File or reparse point' 'Plain directory' 'Choose a plain dedicated Tracker path.' }
    if ($acl) {
        $rootOwner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        $allowedRootOwners = @($request.agentSid)
        if ($account) { $allowedRootOwners += $account.SID.Value }
        if (-not $service) { $allowedRootOwners += 'S-1-5-32-544' }
        if ($rootOwner -notin $allowedRootOwners) { Add-Issue 'TRACKER_ROOT_OWNER' $root $rootOwner ($allowedRootOwners -join ', ') 'A different installed Tracker requires a full reinstall.' }
        if ($account -and $rootOwner -eq $account.SID.Value -and -not $acl.AreAccessRulesProtected) { Add-Issue 'TRACKER_ROOT_INHERITANCE' $root 'Inherited ACL' 'Protected ACL' 'Repair the installed Tracker ACL as administrator.' }
    }
    if ($item -and $item.PSIsContainer) {
        try { $children = @(Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop) }
        catch { Add-Issue 'TRACKER_CHILDREN_UNREADABLE' $root $_.Exception.Message 'Readable immediate children' 'Fix access to the Tracker root.'; $children = @() }
        foreach ($child in $children) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                Add-Issue 'TRACKER_CHILD_REPARSE' $child.FullName 'Reparse point' 'Plain child' 'Remove the reparse point before installation.'
            }
        }
    }
}
$preflightTasksRoot = Join-Path $root 'tasks'
if (Test-Path -LiteralPath $preflightTasksRoot) {
    try { $tasks = Get-Item -LiteralPath $preflightTasksRoot -Force -ErrorAction Stop; $tasksAcl = Get-Acl -LiteralPath $preflightTasksRoot -ErrorAction Stop }
    catch { Add-Issue 'TASKS_UNREADABLE' $preflightTasksRoot $_.Exception.Message 'Plain tasks directory with readable protected ACL' 'Fix access to the tasks directory.'; $tasks = $null; $tasksAcl = $null }
    if ($tasks -and (-not $tasks.PSIsContainer -or ($tasks.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
        Add-Issue 'TASKS_NOT_PLAIN' $preflightTasksRoot 'File or reparse point' 'Plain directory' 'Replace with a plain tasks directory.'
    }
    if ($tasksAcl -and -not $tasksAcl.AreAccessRulesProtected) {
        $scriptPath = (Join-Path $PSScriptRoot 'Protect-TrackerTasks.ps1').Replace("'", "''")
        $quotedRoot = $root.Replace("'", "''")
        $command = "pwsh -NoProfile -File '$scriptPath' -TrackerRoot '$quotedRoot'"
        Add-Issue 'TASKS_INHERITED_ACL' $preflightTasksRoot 'Inherited ACL' 'Protected transitional ACL' "Preview: $command; after READY, apply: $command -Apply"
    }
}
Stop-OnIssues
if ($ValidateOnly) {
    Write-Output "VALID: $root; service: $($request.serviceName)"
    return
}
if ($MigrateTasks) {
    if (-not $service -or -not (Test-Path -LiteralPath $installedConfig)) { throw 'Install the service before migrating existing tasks.' }
    if (Test-Path -LiteralPath (Join-Path $request.protectedRoot 'state\aidev60-metadata-migrated.json')) {
        throw 'Tracker data was already migrated; do not repeat -MigrateTasks.'
    }
    & (Join-Path $PSScriptRoot 'Migrate-TrackerMetadata.ps1') -ConfigPath $installedConfig
    & (Join-Path $PSScriptRoot 'Migrate-TrackerTasks.ps1') -ConfigPath $installedConfig -PendingMetadata
}
if ($MigrateTasks) {
    & (Join-Path (Split-Path -Parent $PSScriptRoot) 'tests\probe-tracker-owner-transition.ps1') `
        -ServiceAccountSid $account.SID.Value
}
Write-Output "Installing Tracker: $root; service: $($request.serviceName); account: $($request.serviceAccountName)"

$password = $null
if (-not $account) {
    $password = Read-Host "Password for $($request.serviceAccountName)" -AsSecureString
    $account = New-LocalUser -Name $request.serviceAccountName -Password $password -Description 'Personal Task Tracker service'
} elseif (-not $service) {
    $password = Read-Host "Password for existing $($request.serviceAccountName)" -AsSecureString
}
Add-Type -Path (Join-Path $PSScriptRoot 'ServiceLogonRight.cs')
$sidBytes = [byte[]]::new($account.SID.BinaryLength)
$account.SID.GetBinaryForm($sidBytes, 0)
[TaskFolderServiceLogonRight]::Grant($sidBytes) | Out-Null
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$serviceSid = $account.SID
$agentSid = [Security.Principal.SecurityIdentifier]$request.agentSid

function Save-ManagedAcl([string]$path, $acl, [bool]$directory, [bool]$ownerChanged) {
    try {
        if ($ownerChanged) {
            $fresh = if ($directory) { [Security.AccessControl.DirectorySecurity]::new() }
                else { [Security.AccessControl.FileSecurity]::new() }
            $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
            $fresh.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm($sections), $sections)
            Set-Acl -LiteralPath $path -AclObject $fresh
        } elseif ($directory) {
            [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $acl)
        } else {
            [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($path), $acl)
        }
    } catch { throw "ACL write failed for ${path}: $($_.Exception.Message)" }
}
function Set-DirectoryRights([string]$path, $owner, [bool]$agentRead) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain directory: $path" }
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    $ownerChanged = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $owner.Value
    if ($ownerChanged) { $acl.SetOwner($owner) }
    foreach ($entry in @(@($system, 'FullControl'), @($admins, 'FullControl'), @($serviceSid, 'FullControl'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($entry[0], $entry[1], 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    if ($agentRead) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agentSid, 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None', 'Allow')) }
    Save-ManagedAcl $path $acl $true $ownerChanged
}
function Set-FileRights([string]$path, [bool]$agentRead) {
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain file: $path" }
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    $ownerChanged = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $serviceSid.Value
    if ($ownerChanged) { $acl.SetOwner($serviceSid) }
    foreach ($entry in @(@($system, 'FullControl'), @($admins, 'FullControl'), @($serviceSid, 'FullControl'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($entry[0], $entry[1], 'Allow'))
    }
    if ($agentRead) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agentSid, 'ReadAndExecute', 'Allow')) }
    Save-ManagedAcl $path $acl $false $ownerChanged
}

$parent = [IO.Path]::GetDirectoryName($root)
$missing = @()
for ($path = $parent; -not (Test-Path -LiteralPath $path); $path = [IO.Path]::GetDirectoryName($path)) { $missing = @($path) + $missing }
foreach ($path in $missing) { Set-DirectoryRights $path $admins $true }
# Freeze the current ACL of each immediate child before replacing the root's inherited ACL.
if (Test-Path -LiteralPath $root) {
    foreach ($child in Get-ChildItem -LiteralPath $root -Force) {
        if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Tracker child is a reparse point: $($child.FullName)" }
        $childAcl = Get-Acl -LiteralPath $child.FullName
        if (-not $childAcl.AreAccessRulesProtected) {
            $childAcl.SetAccessRuleProtection($true, $true)
            Set-Acl -LiteralPath $child.FullName -AclObject $childAcl
        }
    }
}
Set-DirectoryRights $root $serviceSid $true
if (Test-Path -LiteralPath $request.tasksRoot) {
    $tasks = Get-Item -LiteralPath $request.tasksRoot -Force
    $tasksAcl = Get-Acl -LiteralPath $request.tasksRoot
    if (-not $tasks.PSIsContainer -or ($tasks.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        -not $tasksAcl.AreAccessRulesProtected) { throw 'Existing tasks directory must retain its protected transitional ACL.' }
} else { Set-DirectoryRights $request.tasksRoot $serviceSid $true }
Set-DirectoryRights $request.protectedRoot $serviceSid $false
foreach ($name in @('bin', 'state', 'logs')) { Set-DirectoryRights (Join-Path $request.protectedRoot $name) $serviceSid $false }

$repo = Split-Path -Parent $PSScriptRoot
if ($service -and $service.Status -eq 'Running') { Stop-Service -Name $request.serviceName -ErrorAction Stop }
& $csc /nologo /target:exe "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs')
if ($LASTEXITCODE -ne 0) { throw 'Service host compilation failed.' }
foreach ($name in @('folder-worker.js', 'task-folder.js', 'Set-TaskDirectoryAcl.ps1')) {
    Copy-Item -LiteralPath (Join-Path $repo "src\$name") -Destination $request.installRoot -Force
}
foreach ($name in @('TaskTrackerService.exe', 'folder-worker.js', 'task-folder.js', 'Set-TaskDirectoryAcl.ps1')) { Set-FileRights (Join-Path $request.installRoot $name) $false }
Copy-Item -LiteralPath (Join-Path $repo 'src\mcp-adapter.js') -Destination $root -Force
Set-FileRights (Join-Path $root 'mcp-adapter.js') $true
$clientConfig = Join-Path $root 'tracker-client.json'
[IO.File]::WriteAllText($clientConfig, ((@{ pipeName = $request.pipeName } | ConvertTo-Json -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
Set-FileRights $clientConfig $true
$manifestPath = Join-Path $root 'projects.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    [IO.File]::WriteAllText($manifestPath, '{"schema_version":1,"projects":[]}' + "`n", [Text.UTF8Encoding]::new($false))
}
Set-FileRights $manifestPath $false
$config = [ordered]@{
    schemaVersion = 1; trackerRoot = $root; serviceName = $request.serviceName
    serviceAccountSid = $serviceSid.Value; agentSid = $agentSid.Value; pipeName = $request.pipeName
    nodePath = $nodePath; pwshPath = $pwshPath; tasksRoot = $request.tasksRoot
    protectedRoot = $request.protectedRoot; installRoot = $request.installRoot
}
[IO.File]::WriteAllText($installedConfig, (($config | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
Set-FileRights $installedConfig $false
$aclBackupPath = $null
if ($MigrateTasks) {
    $legacy = Get-Service -Name TaskFolderMcp -ErrorAction SilentlyContinue
    $legacyWasRunning = $legacy -and $legacy.Status -eq 'Running'
    if ($legacyWasRunning) { Stop-Service -Name TaskFolderMcp -ErrorAction Stop }
    try {
        & (Join-Path $PSScriptRoot 'Migrate-TrackerMetadata.ps1') -ConfigPath $installedConfig -Apply
        $migrationOutput = @(& (Join-Path $PSScriptRoot 'Migrate-TrackerTasks.ps1') -ConfigPath $installedConfig -Apply)
        $migrationOutput | Write-Output
        $finished = @($migrationOutput | Where-Object { $_ -match '^MIGRATED: .*; ACL backup (.+)$' })
        if ($finished.Count -ne 1) { throw 'Task migration did not report its ACL backup.' }
        $aclBackupPath = [regex]::Match($finished[0], 'ACL backup (.+)$').Groups[1].Value
    } catch {
        $failure = $_
        if ($failure.Exception.Message -like '*TASK_ACL_ROLLBACK_INCOMPLETE*') { throw $failure }
        $metadataRestored = $false
        try {
            & (Join-Path $PSScriptRoot 'Migrate-TrackerMetadata.ps1') -ConfigPath $installedConfig -Rollback
            $metadataRestored = $true
        } catch { Write-Warning "Metadata rollback failed: $($_.Exception.Message)" }
        if ($metadataRestored -and $legacyWasRunning) {
            try { Start-Service -Name TaskFolderMcp -ErrorAction Stop }
            catch { Write-Warning "Legacy service restart failed: $($_.Exception.Message)" }
        }
        if (-not $metadataRestored) { throw "RECOVERY_INCOMPLETE: metadata rollback failed after $failure" }
        throw $failure
    }
}
if (-not $service) {
    $credential = [pscredential]::new(".\$($request.serviceAccountName)", $password)
    New-Service -Name $request.serviceName -BinaryPathName "`"$exe`" --config `"$installedConfig`"" `
        -Credential $credential -StartupType Automatic -Description 'Personal Task Tracker service' | Out-Null
} else {
    Set-Service -Name $request.serviceName -StartupType Automatic -ErrorAction Stop
}
try { Start-Service -Name $request.serviceName -ErrorAction Stop }
catch {
    $failure = $_
    if ($MigrateTasks -and $aclBackupPath) {
        $aclRestored = $false
        try {
            & (Join-Path $PSScriptRoot 'Migrate-TrackerTasks.ps1') -ConfigPath $installedConfig -Rollback -BackupPath $aclBackupPath
            $aclRestored = $true
        } catch { Write-Warning "Task ACL rollback failed: $($_.Exception.Message)" }
        if ($aclRestored) {
            try { & (Join-Path $PSScriptRoot 'Migrate-TrackerMetadata.ps1') -ConfigPath $installedConfig -Rollback }
            catch { Write-Warning "Metadata rollback failed: $($_.Exception.Message)"; $aclRestored = $false }
        }
        if ($aclRestored -and $legacyWasRunning) {
            try { Start-Service -Name TaskFolderMcp -ErrorAction Stop }
            catch { Write-Warning "Legacy service restart failed: $($_.Exception.Message)" }
        }
    }
    throw $failure
}
$installedService = Get-Service -Name $request.serviceName -ErrorAction Stop
if ($installedService.Status -ne 'Running' -or $installedService.StartType -ne 'Automatic') {
    throw "Service did not reach Running/Automatic: $($installedService.Status)/$($installedService.StartType)"
}
Write-Output "Installed: $($request.serviceName); Tracker: $root"
