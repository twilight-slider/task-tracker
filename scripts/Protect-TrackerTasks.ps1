param(
    [Parameter(Mandatory)][string]$TrackerRoot,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathFullyQualified($TrackerRoot)) { throw 'TrackerRoot must be absolute.' }
$tracker = [IO.Path]::GetFullPath($TrackerRoot).TrimEnd('\')
$tasks = Join-Path $tracker 'tasks'
foreach ($path in @($tracker, $tasks)) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Not a plain directory: $path"
    }
}
$original = Get-Acl -LiteralPath $tasks
if ($original.AreAccessRulesProtected) { Write-Output "ALREADY PROTECTED: $tasks"; return }
$proposed = Get-Acl -LiteralPath $tasks
$proposed.SetAccessRuleProtection($true, $true)
$children = @{}
foreach ($child in Get-ChildItem -LiteralPath $tasks -Force) {
    if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in tasks: $($child.FullName)" }
    $children[$child.FullName] = (Get-Acl -LiteralPath $child.FullName).GetSecurityDescriptorSddlForm('Access, Owner, Group')
}
if (-not $Apply) { Write-Output "READY: $tasks; freeze inherited ACL; $($children.Count) immediate child ACLs to verify"; return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Apply requires an elevated PowerShell window.'
}
$reportDir = Join-Path (Split-Path -Parent $PSScriptRoot) '.runtime\tests\protect-tracker-tasks'
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$backup = Join-Path $reportDir ('before-' + [guid]::NewGuid().ToString('N') + '.sddl')
[IO.File]::WriteAllText($backup, $original.GetSecurityDescriptorSddlForm('Access, Owner, Group'), [Text.UTF8Encoding]::new($false))
try {
    Set-Acl -LiteralPath $tasks -AclObject $proposed
    if (-not (Get-Acl -LiteralPath $tasks).AreAccessRulesProtected) { throw 'Tasks ACL verification failed.' }
    foreach ($path in $children.Keys) {
        if ((Get-Acl -LiteralPath $path).GetSecurityDescriptorSddlForm('Access, Owner, Group') -ne $children[$path]) {
            throw "Child ACL changed: $path"
        }
    }
} catch {
    $reason = $_.Exception.Message
    try { Set-Acl -LiteralPath $tasks -AclObject $original }
    catch { throw "Tasks ACL update failed ($reason), and rollback failed: $($_.Exception.Message); backup: $backup" }
    throw "Tasks ACL update rolled back: $reason; backup: $backup"
}
Write-Output "PROTECTED: $tasks; child ACLs unchanged; backup $backup"
