param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$TaskFolder,
    [switch]$Migration
)

$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$tasksRoot = [IO.Path]::GetFullPath([string]$config.tasksRoot).TrimEnd('\')
$task = [IO.Path]::GetFullPath($TaskFolder).TrimEnd('\')
$relative = [IO.Path]::GetRelativePath($tasksRoot, $task)
if ($config.schemaVersion -ne 1 -or $relative -notmatch '^\d{4}\\[A-Z][A-Z0-9_-]*-[1-9][0-9]*$' -or
    -not $config.serviceAccountSid -or -not $config.agentSid) {
    throw 'Task ACL request does not match the installed service configuration.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (($Migration -and -not $elevated) -or (-not $Migration -and $config.serviceAccountSid -ne $identity.User.Value)) {
    throw 'Task ACL changes require the service account or an elevated migration.'
}
$service = [Security.Principal.SecurityIdentifier]$config.serviceAccountSid
$agent = [Security.Principal.SecurityIdentifier]$config.agentSid
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'

function Set-TaskAcl([string]$path, [bool]$ordinary, [bool]$taskContent) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain directory: $path" }
    $current = Get-Acl -LiteralPath $path
    if (-not $Migration -and $current.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $service.Value) {
        throw "Directory is not service-owned: $path"
    }
    $acl = $current
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    if ($Migration) { $acl.SetOwner($service) }
    foreach ($sid in @($system, $admins, $service)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    if ($ordinary) {
        $rights = if ($taskContent) { 'ReadAndExecute, CreateFiles' } else { 'ReadAndExecute' }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agent, $rights, 'None', 'None', 'Allow'))
        if ($taskContent) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $agent, 'FullControl', 'ObjectInherit', 'InheritOnly', 'Allow'))
        }
    }
    try { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $acl) }
    catch { throw "ACL write failed for ${path}: $($_.Exception.Message)" }
}

Set-TaskAcl $tasksRoot $true $false
$year = [IO.Path]::GetDirectoryName($task)
Set-TaskAcl $year $true $false
Set-TaskAcl $task $true $true
foreach ($dir in Get-ChildItem -LiteralPath $task -Directory -Recurse -Force) {
    $inside = [IO.Path]::GetRelativePath($task, $dir.FullName)
    $protected = $inside -eq '.protected' -or $inside.StartsWith('.protected\', [StringComparison]::OrdinalIgnoreCase)
    Set-TaskAcl $dir.FullName (-not $protected) (-not $protected)
}
