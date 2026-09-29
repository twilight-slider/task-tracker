param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [switch]$Apply,
    [switch]$PendingMetadata,
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
        $acl = Get-Acl -LiteralPath $entry.path
        $acl.SetSecurityDescriptorSddlForm([string]$entry.sddl, $sections)
        if ($entry.directory) { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($entry.path), $acl) }
        else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($entry.path), $acl) }
    }
    Write-Output "ROLLED BACK: task ACLs from $backupFile"
    return
}
if ($Apply -and $PendingMetadata) { throw 'PendingMetadata is only valid for dry-run.' }
if (-not $PendingMetadata) {
    if (Test-Path -LiteralPath (Join-Path $tasks 'AGENTS.md')) { throw 'Move tasks/AGENTS.md to Tracker root before migration.' }
    if (Test-Path -LiteralPath (Join-Path $tasks 'projects.json')) { throw 'Move tasks/projects.json to Tracker root before migration.' }
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
foreach ($item in @($directories) + @($files)) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in tasks: $($item.FullName)" }
}
$years = @(Get-ChildItem -LiteralPath $tasks -Directory -Force)
if (@($years | Where-Object Name -NotMatch '^\d{4}$').Count) { throw 'Unexpected directory directly under tasks.' }
$taskFolders = @($years | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Directory -Force })
$nonstandard = @($taskFolders | Where-Object Name -NotMatch '^[A-Z][A-Z0-9_-]*-[1-9][0-9]*$')
Write-Output "READY: $($taskFolders.Count) task folders ($($nonstandard.Count) nonstandard); $($directories.Count) directories; $($files.Count) files"
foreach ($folder in $nonstandard) { Write-Output "NONSTANDARD: $($folder.FullName)" }
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
function Set-ParentAcl([string]$path) {
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Invalid task parent: $path"
    }
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    $acl.SetOwner($service)
    foreach ($sid in @($system, $admins, $service)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agent, 'ReadAndExecute', 'None', 'None', 'Allow'))
    [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $acl)
}
try {
    Set-ParentAcl $tasks
    foreach ($year in $years) { Set-ParentAcl $year.FullName }
    foreach ($folder in $taskFolders) {
        if ($folder.Name -match '^[A-Z][A-Z0-9_-]*-[1-9][0-9]*$') {
            foreach ($name in @('.protected', '.protected\snapshots')) {
                $path = Join-Path $folder.FullName $name
                if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
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
        $acl = Get-Acl -LiteralPath $item.FullName
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
        if ($protectedFile) { $acl.SetOwner($service) }
        foreach ($sid in @($system, $admins, $service)) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow'))
        }
        if (-not $protectedFile) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agent, 'FullControl', 'Allow'))
        }
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($item.FullName), $acl)
    }
} catch {
    $failure = $_
    $rollbackFailures = 0
    foreach ($entry in @($backup | Sort-Object { $_.path.Length } -Descending)) {
        try {
            $acl = Get-Acl -LiteralPath $entry.path
            $acl.SetSecurityDescriptorSddlForm($entry.sddl, $sections)
            if ($entry.directory) {
                [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($entry.path), $acl)
            } else {
                [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($entry.path), $acl)
            }
        } catch {
            $rollbackFailures += 1
            Write-Warning "ACL rollback failed: $($entry.path): $($_.Exception.Message)"
        }
    }
    if ($rollbackFailures) { throw "TASK_ACL_ROLLBACK_INCOMPLETE: $rollbackFailures paths; backup $backupPath. $failure" }
    throw "Task migration failed; previous ACLs restored. Backup: $backupPath. $failure"
}
Write-Output "MIGRATED: $($taskFolders.Count) tasks; ACL backup $backupPath"
