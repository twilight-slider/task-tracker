param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [switch]$Apply,
    [switch]$PendingMetadata,
    [switch]$ImportExisting,
    [switch]$Rollback,
    [string]$BackupPath
)

$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$root = [IO.Path]::GetFullPath([string]$config.trackerRoot).TrimEnd('\')
$tasks = [IO.Path]::GetFullPath([string]$config.tasksRoot).TrimEnd('\')
if ($config.schemaVersion -ne 1 -or $tasks -ne (Join-Path $root 'tasks') -or
    $config.protectedRoot -ne (Join-Path $root '.protected') -or
    -not $config.serviceAccountSid -or -not $config.agentSid -or
    -not (Test-Path -LiteralPath $tasks -PathType Container)) { throw 'Invalid installed Tracker task layout.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (($Apply -or $Rollback) -and -not $admin) { throw 'Task migration requires elevated PowerShell.' }
if ($Apply -and $Rollback) { throw 'Choose migration or rollback.' }
function Restore-SavedAcl($entry, $sections) {
    $current = Get-Acl -LiteralPath $entry.path
    $saved = if ($entry.directory) { [Security.AccessControl.DirectorySecurity]::new() }
        else { [Security.AccessControl.FileSecurity]::new() }
    $saved.SetSecurityDescriptorSddlForm([string]$entry.sddl, $sections)
    $targetOwner = $saved.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($current.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $targetOwner) {
        $fresh = if ($entry.directory) { [Security.AccessControl.DirectorySecurity]::new() }
            else { [Security.AccessControl.FileSecurity]::new() }
        $ownerAndAccess = [Security.AccessControl.AccessControlSections]'Owner,Access'
        $fresh.SetSecurityDescriptorSddlForm($current.GetSecurityDescriptorSddlForm($ownerAndAccess), $ownerAndAccess)
        $fresh.SetOwner([Security.Principal.SecurityIdentifier]$targetOwner)
        Set-Acl -LiteralPath $entry.path -AclObject $fresh
    }
    $current = Get-Acl -LiteralPath $entry.path
    $current.SetSecurityDescriptorSddlForm([string]$entry.sddl,
        [Security.AccessControl.AccessControlSections]'Group,Access')
    if ($entry.directory) { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($entry.path), $current) }
    else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($entry.path), $current) }
    if ((Get-Acl -LiteralPath $entry.path).GetSecurityDescriptorSddlForm($sections) -ne [string]$entry.sddl) {
        throw "ACL rollback verification failed: $($entry.path)"
    }
}
if ($Rollback) {
    $backupRoot = [IO.Path]::GetFullPath((Join-Path $config.protectedRoot 'state\acl-backups'))
    if (-not $BackupPath -or -not [IO.Path]::IsPathFullyQualified($BackupPath)) { throw 'An absolute ACL backup path is required.' }
    $backupFile = [IO.Path]::GetFullPath($BackupPath)
    if (-not $backupFile.StartsWith(($backupRoot.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'ACL backup must be inside this Tracker protected state.'
    }
    foreach ($name in @('TaskFolderMcp', [string]$config.serviceName)) {
        if ((Get-Service -Name $name -ErrorAction SilentlyContinue).Status -eq 'Running') {
            throw "Stop $name before ACL rollback."
        }
    }
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Group,Access'
    $saved = @(Get-Content -LiteralPath $backupFile -Raw | ConvertFrom-Json)
    foreach ($entry in $saved) {
        $path = [IO.Path]::GetFullPath([string]$entry.path)
        if (-not $path.Equals($tasks, [StringComparison]::OrdinalIgnoreCase) -and
            -not $path.StartsWith(($tasks + '\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw "ACL backup includes a path outside tasks: $path"
        }
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.PSIsContainer -ne [bool]$entry.directory) {
            throw "ACL rollback target changed type: $path"
        }
    }
    foreach ($entry in @($saved | Sort-Object { $_.path.Length } -Descending)) {
        Restore-SavedAcl $entry $sections
    }
    Write-Output "ROLLED BACK: task ACLs from $backupFile"
    return
}
if ($Apply -and $PendingMetadata) { throw 'PendingMetadata is only valid for dry-run.' }
if ($ImportExisting -and $PendingMetadata) { throw 'Choose legacy metadata migration or existing-task import.' }
if (-not $PendingMetadata) {
    if (-not $ImportExisting -and (Test-Path -LiteralPath (Join-Path $tasks 'AGENTS.md'))) {
        throw 'Move tasks/AGENTS.md to Tracker root before migration.'
    }
    if (Test-Path -LiteralPath (Join-Path $tasks 'projects.json')) { throw 'Move tasks/projects.json to Tracker root before migration.' }
}
if ($ImportExisting) {
    $installedConfig = Join-Path $config.protectedRoot 'service.json'
    $manifestPath = Join-Path $root 'projects.json'
    if ([IO.Path]::GetFullPath($ConfigPath) -ne $installedConfig -or
        -not (Test-Path -LiteralPath (Join-Path $root 'projects.json') -PathType Leaf)) {
        throw 'Existing-task import requires the installed service config and root projects.json.'
    }
    $validator = "const fs=require('node:fs');const folder=require(process.argv[1]);folder.validateManifest(JSON.parse(fs.readFileSync(process.argv[2],'utf8').replace(/^\uFEFF/,'')));"
    & $config.nodePath -e $validator (Join-Path (Split-Path -Parent $PSScriptRoot) 'src\task-folder.js') $manifestPath
    if ($LASTEXITCODE -ne 0) { throw 'Existing root projects.json failed service validation.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
}
if ($Apply -and (Get-Service -Name TaskFolderMcp -ErrorAction SilentlyContinue).Status -eq 'Running') {
    throw 'Stop the legacy TaskFolderMcp service before task migration.'
}
if ($Apply -and (Get-Service -Name $config.serviceName -ErrorAction SilentlyContinue).Status -eq 'Running') {
    throw 'Stop the new Tracker service before task migration.'
}
$directories = @((Get-Item -LiteralPath $tasks -Force))
$directories += @(Get-ChildItem -LiteralPath $tasks -Directory -Recurse -Force)
$files = @(Get-ChildItem -LiteralPath $tasks -File -Recurse -Force)
$noncanonical = @()
foreach ($item in @($directories) + @($files)) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in tasks: $($item.FullName)" }
    if (-not (Get-Acl -LiteralPath $item.FullName).AreAccessRulesCanonical) { $noncanonical += $item.FullName }
}
$years = @(Get-ChildItem -LiteralPath $tasks -Directory -Force)
if (@($years | Where-Object Name -NotMatch '^\d{4}$').Count) { throw 'Unexpected directory directly under tasks.' }
$taskFolders = @($years | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Directory -Force })
$nonstandard = @($taskFolders | Where-Object Name -NotMatch '^[A-Z][A-Z0-9_-]*-[1-9][0-9]*$')
if ($ImportExisting) {
    $registered = @($manifest.projects | ForEach-Object project_key)
    $unregistered = @($taskFolders | Where-Object {
        $_.Name -match '^([A-Z][A-Z0-9_-]*)-[1-9][0-9]*$' -and $Matches[1] -notin $registered
    })
    if ($unregistered.Count) {
        throw "Existing tasks have unregistered project keys: $($unregistered.FullName -join ', ')"
    }
}
Write-Output "READY: $($taskFolders.Count) task folders ($($nonstandard.Count) nonstandard); $($directories.Count) directories; $($files.Count) files"
foreach ($folder in $nonstandard) { Write-Output "NONSTANDARD: $($folder.FullName)" }
foreach ($path in $noncanonical) { Write-Output "NONCANONICAL ACL: $path" }
if (-not $Apply) { return }

$sections = [Security.AccessControl.AccessControlSections]'Owner,Group,Access'
$backup = @(@($directories) + @($files) | ForEach-Object {
    @{ path = $_.FullName; directory = $_.PSIsContainer;
       sddl = (Get-Acl -LiteralPath $_.FullName).GetSecurityDescriptorSddlForm($sections) }
})
$backupRoot = Join-Path $config.protectedRoot 'state\acl-backups'
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$backupPath = Join-Path $backupRoot ('task-acl-before-' + [guid]::NewGuid().ToString('N') + '.json')
$bytes = [Text.UTF8Encoding]::new($false).GetBytes(($backup | ConvertTo-Json -Depth 4))
$handle = [IO.File]::Open($backupPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $handle.Write($bytes, 0, $bytes.Length) } finally { $handle.Dispose() }
$service = [Security.Principal.SecurityIdentifier]$config.serviceAccountSid
$agent = [Security.Principal.SecurityIdentifier]$config.agentSid
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
function Save-MigratedAcl([string]$path, $acl, [bool]$directory, [bool]$ownerChanged) {
    if ($ownerChanged) {
        $fresh = if ($directory) { [Security.AccessControl.DirectorySecurity]::new() }
            else { [Security.AccessControl.FileSecurity]::new() }
        $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
        $fresh.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm($sections), $sections)
        Set-Acl -LiteralPath $path -AclObject $fresh
    } elseif ($directory) { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $acl) }
    else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($path), $acl) }
}
function Set-ParentAcl([string]$path) {
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Invalid task parent: $path"
    }
    $current = Get-Acl -LiteralPath $path
    $currentOwner = $current.GetOwner([Security.Principal.SecurityIdentifier])
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($service)
    foreach ($sid in @($system, $admins, $service)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agent, 'ReadAndExecute', 'None', 'None', 'Allow'))
    try {
        if (-not $current.AreAccessRulesCanonical) {
            $canonical = [Security.AccessControl.DirectorySecurity]::new()
            $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
            $canonical.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm($sections), $sections)
            $canonical.SetOwner($currentOwner)
            [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $canonical)
        }
        if ($currentOwner.Value -ne $service.Value) { Set-Acl -LiteralPath $path -AclObject $acl }
        else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $acl) }
    }
    catch { throw "Parent ACL write failed for ${path}: $($_.Exception.Message)" }
}
$createdDirectories = [Collections.Generic.List[string]]::new()
try {
    Set-ParentAcl $tasks
    foreach ($year in $years) { Set-ParentAcl $year.FullName }
    foreach ($folder in $taskFolders) {
        if ($folder.Name -match '^[A-Z][A-Z0-9_-]*-[1-9][0-9]*$') {
            foreach ($name in @('.protected', '.protected\snapshots')) {
                $path = Join-Path $folder.FullName $name
                if (-not (Test-Path -LiteralPath $path)) {
                    New-Item -ItemType Directory -Path $path | Out-Null
                    $createdDirectories.Add($path)
                }
                $item = Get-Item -LiteralPath $path -Force
                if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    throw "Invalid protected task directory: $path"
                }
            }
        }
        & (Join-Path (Split-Path -Parent $PSScriptRoot) 'src\Set-TaskDirectoryAcl.ps1') `
            -ConfigPath $ConfigPath -TaskFolder $folder.FullName -Migration
    }
    foreach ($item in $files) {
        $relative = [IO.Path]::GetRelativePath($tasks, $item.FullName)
        $protectedFile = $relative -match '^\d{4}\\[^\\]+\\\.protected\\'
        $current = Get-Acl -LiteralPath $item.FullName
        $currentOwner = $current.GetOwner([Security.Principal.SecurityIdentifier])
        $acl = if ($current.AreAccessRulesCanonical) { $current }
            else { [Security.AccessControl.FileSecurity]::new() }
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
        $ownerChanged = $protectedFile -and $currentOwner.Value -ne $service.Value
        if ($ownerChanged) {
            $acl.SetOwner($service)
        } elseif (-not $current.AreAccessRulesCanonical) {
            $acl.SetOwner($currentOwner)
        }
        foreach ($sid in @($system, $admins, $service)) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow'))
        }
        if (-not $protectedFile) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agent, 'FullControl', 'Allow'))
        }
        if (-not $current.AreAccessRulesCanonical) {
            $canonical = [Security.AccessControl.FileSecurity]::new()
            $access = [Security.AccessControl.AccessControlSections]'Owner,Access'
            $canonical.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm($access), $access)
            $canonical.SetOwner($currentOwner)
            [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($item.FullName), $canonical)
        }
        Save-MigratedAcl $item.FullName $acl $false $ownerChanged
    }
} catch {
    $failure = $_
    $rollbackFailures = 0
    foreach ($entry in @($backup | Sort-Object { $_.path.Length } -Descending)) {
        try {
            Restore-SavedAcl $entry $sections
        } catch {
            $rollbackFailures += 1
            Write-Warning "ACL rollback failed: $($entry.path): $($_.Exception.Message)"
        }
    }
    foreach ($path in @($createdDirectories | Sort-Object Length -Descending)) {
        try {
            if (@(Get-ChildItem -LiteralPath $path -Force).Count) { throw 'Directory is no longer empty.' }
            Remove-Item -LiteralPath $path -Force
        } catch {
            $rollbackFailures += 1
            Write-Warning "Created directory cleanup failed: $($path): $($_.Exception.Message)"
        }
    }
    if ($rollbackFailures) { throw "TASK_ACL_ROLLBACK_INCOMPLETE: $rollbackFailures paths; backup $backupPath. $failure" }
    throw "Task migration failed; previous ACLs restored. Backup: $backupPath. $failure"
}
Write-Output "MIGRATED: $($taskFolders.Count) tasks; ACL backup $backupPath"
