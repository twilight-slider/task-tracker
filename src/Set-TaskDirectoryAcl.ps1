param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$TaskFolder,
    [switch]$Migration
)

$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'WorkspaceAgents.ps1')
$matrix = @(Get-TrackerTaskAclMatrix $config)
$tasksRoot = [IO.Path]::GetFullPath([string]$config.tasksRoot).TrimEnd('\')
$task = [IO.Path]::GetFullPath($TaskFolder).TrimEnd('\')
$relative = [IO.Path]::GetRelativePath($tasksRoot, $task)
if ($config.schemaVersion -ne 2 -or
    ($Migration -and $relative -notmatch '^\d{4}\\[^\\]+$') -or
    (-not $Migration -and $relative -notmatch '^\d{4}\\[A-Z][A-Z0-9_-]*-[1-9][0-9]*$') -or
    -not $config.serviceAccountSid -or -not $config.agentSid) {
    throw 'Task ACL request does not match the installed service configuration.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (($Migration -and -not $elevated) -or
    (-not $Migration -and -not $elevated -and $config.serviceAccountSid -ne $identity.User.Value)) {
    throw 'Task ACL changes require the service account or an elevated migration.'
}
$service = [Security.Principal.SecurityIdentifier]$config.serviceAccountSid
$agent = [Security.Principal.SecurityIdentifier]$config.agentSid
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'

$ancestor = $task
while ($ancestor) {
    $item = Get-Item -LiteralPath $ancestor -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Not a plain directory: $ancestor"
    }
    $ancestor = [IO.Path]::GetDirectoryName($ancestor)
}

function Set-TaskAcl([string]$path, [bool]$ordinary, [bool]$taskContent) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain directory: $path" }
    $current = Get-Acl -LiteralPath $path
    if (-not $Migration -and $current.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $service.Value) {
        throw "Directory is not service-owned: $path"
    }
    $acl = if ($Migration) { [Security.AccessControl.DirectorySecurity]::new() } else { $current }
    $acl.SetAccessRuleProtection($true, $false)
    if ($Migration) { $acl.SetOwner($service) }
    else { foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) } }
    foreach ($row in $matrix) {
        $sid = [Security.Principal.SecurityIdentifier]$row.sid
        if ($row.protectedDirectories) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $sid, $row.protectedDirectories, 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
        } elseif ($ordinary) {
            $rights = if ($taskContent) { $row.taskDirectories } else { $row.treeDirectories }
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, $rights, 'None', 'None', 'Allow'))
            if ($taskContent) {
                $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                    $sid, $row.taskFiles, 'ObjectInherit', 'InheritOnly', 'Allow'))
            }
        }
    }
    try {
        if ($Migration) {
            $currentOwner = $current.GetOwner([Security.Principal.SecurityIdentifier])
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
        else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $acl) }
    }
    catch { throw "ACL write failed for ${path}: $($_.Exception.Message)" }
}

$year = [IO.Path]::GetDirectoryName($task)
$ordinaryDirectories = [Collections.Generic.List[string]]::new()
$privateDirectories = [Collections.Generic.List[string]]::new()
$pending = [Collections.Generic.Stack[string]]::new()
$pending.Push($task)
while ($pending.Count) {
    $path = $pending.Pop()
    foreach ($dir in Get-ChildItem -LiteralPath $path -Directory -Force) {
        if ($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Not a plain directory: $($dir.FullName)" }
        $inside = [IO.Path]::GetRelativePath($task, $dir.FullName)
        if (@($inside.Split('\') | Where-Object { $_ -ieq '.protected' }).Count) {
            $privateDirectories.Add($dir.FullName)
        } else { $ordinaryDirectories.Add($dir.FullName) }
        $pending.Push($dir.FullName)
    }
}
# Закрываем наследование новых private-каталогов до выдачи прав их родителям.
foreach ($path in $privateDirectories) {
    $privateAcl = Get-Acl -LiteralPath $path
    if (-not $privateAcl.AreAccessRulesProtected) { Set-TaskAcl $path $false $false }
}
if (-not $Migration) {
    Set-TaskAcl $tasksRoot $true $false
    Set-TaskAcl $year $true $false
}
Set-TaskAcl $task $true $true
foreach ($path in $ordinaryDirectories) { Set-TaskAcl $path $true $true }
