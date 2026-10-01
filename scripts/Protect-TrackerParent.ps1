param(
    [Parameter(Mandatory)][string]$TrackerRoot,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathFullyQualified($TrackerRoot)) { throw 'TrackerRoot must be absolute.' }
$tracker = [IO.Path]::GetFullPath($TrackerRoot).TrimEnd('\')
$parent = [IO.Path]::GetDirectoryName($tracker)
if (-not $parent -or $parent -eq [IO.Path]::GetPathRoot($tracker)) {
    throw 'Tracker must have a dedicated parent below the volume root.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
foreach ($path in @($parent, $tracker)) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain directory: $path" }
}
$original = Get-Acl -LiteralPath $parent
$owner = $original.GetOwner([Security.Principal.SecurityIdentifier]).Value
$trackerOwner = (Get-Acl -LiteralPath $tracker).GetOwner([Security.Principal.SecurityIdentifier]).Value
if ($owner -notin @($identity.User.Value, $trackerOwner, $admins.Value)) { throw "Unexpected parent owner: $owner" }
# NTFS ACEs may retain GENERIC_* bits; FileSystemAccessRule accepts only file-specific rights.
function Get-MappedRights([Security.AccessControl.FileSystemRights]$rights) {
    $mask = ([long][int]$rights) -band [long]4294967295
    $mapped = $mask -band [long]268435455
    if ($mask -band [long]2147483648) { $mapped = $mapped -bor [long][int][Security.AccessControl.FileSystemRights]::Read -bor [long][int][Security.AccessControl.FileSystemRights]::Synchronize }
    if ($mask -band [long]1073741824) { $mapped = $mapped -bor [long][int][Security.AccessControl.FileSystemRights]::Write -bor [long][int][Security.AccessControl.FileSystemRights]::ReadPermissions -bor [long][int][Security.AccessControl.FileSystemRights]::Synchronize }
    if ($mask -band [long]536870912) { $mapped = $mapped -bor [long][int][Security.AccessControl.FileSystemRights]::ExecuteFile -bor [long][int][Security.AccessControl.FileSystemRights]::ReadAttributes -bor [long][int][Security.AccessControl.FileSystemRights]::ReadPermissions -bor [long][int][Security.AccessControl.FileSystemRights]::Synchronize }
    if ($mask -band [long]268435456) { $mapped = $mapped -bor [long][int][Security.AccessControl.FileSystemRights]::FullControl }
    if ($mapped -band (-bnot [long][int][Security.AccessControl.FileSystemRights]::FullControl)) { throw "Unsupported ACL rights mask: $mask" }
    return [Security.AccessControl.FileSystemRights][int]$mapped
}
if ($owner -eq $admins.Value -and $original.AreAccessRulesProtected) {
    $danger = [int][Security.AccessControl.FileSystemRights]'ChangePermissions, TakeOwnership, DeleteSubdirectoriesAndFiles, Delete'
    foreach ($rule in $original.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin @($admins.Value, $system.Value) -and $rule.AccessControlType -eq 'Allow' -and
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::InheritOnly -and
            (([int](Get-MappedRights $rule.FileSystemRights)) -band $danger)) { throw "Protected parent still has dangerous grant: $sid" }
    }
    Write-Output "ALREADY PROTECTED: $parent"
    return
}

# Keep each inherited grant for children, while removing delete/ACL rights on the parent itself.
$remove = [int][Security.AccessControl.FileSystemRights]'ChangePermissions, TakeOwnership, DeleteSubdirectoriesAndFiles, Delete'
$proposed = [Security.AccessControl.DirectorySecurity]::new()
$proposed.SetAccessRuleProtection($true, $false)
$proposed.SetOwner($admins)
foreach ($rule in $original.Access) {
    $sid = try { $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]) } catch { throw "Cannot resolve ACL principal $($rule.IdentityReference)" }
    if ($rule.AccessControlType -eq 'Deny' -and (([long][int]$rule.FileSystemRights -band [long]4294967295) -band [long]4026531840)) {
        throw "Generic deny ACE requires manual review: $sid"
    }
    $rights = Get-MappedRights $rule.FileSystemRights
    $privileged = $sid.Value -in @($admins.Value, $system.Value)
    if ($privileged -or $rule.AccessControlType -eq 'Deny') {
        $proposed.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $rights, $rule.InheritanceFlags, $rule.PropagationFlags, $rule.AccessControlType))
        continue
    }
    $safe = [Security.AccessControl.FileSystemRights]([int]$rights -band (-bnot $remove))
    if ([int]$safe) {
        $proposed.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, $safe, 'None', 'None', 'Allow'))
    }
    if ($rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None) {
        $propagation = [Security.AccessControl.PropagationFlags]([int]$rule.PropagationFlags -bor
            [int][Security.AccessControl.PropagationFlags]::InheritOnly)
        $proposed.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $rights, $rule.InheritanceFlags, $propagation, 'Allow'))
    }
}
foreach ($rule in $proposed.Access) {
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin @($admins.Value, $system.Value) -and $rule.AccessControlType -eq 'Allow' -and
        $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::InheritOnly -and
        (([int]$rule.FileSystemRights) -band $remove)) { throw "Unsafe proposed parent grant: $sid" }
}
$children = @(Get-ChildItem -LiteralPath $parent -Force)
foreach ($child in $children) {
    if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in parent: $($child.FullName)" }
}
if (-not $Apply) {
    Write-Output "READY: $parent owner $owner -> Administrators; $($children.Count) immediate children keep inherited grants"
    return
}
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Apply requires an elevated PowerShell window.'
}
$repo = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $repo '.runtime\tests\protect-tracker-parent'
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($parent))).Substring(0, 12).ToLowerInvariant()
$backup = Join-Path $reportDir "$hash-$([guid]::NewGuid().ToString('N'))-before.sddl"
[IO.File]::WriteAllText($backup, $original.GetSecurityDescriptorSddlForm('Access, Owner, Group'), [Text.UTF8Encoding]::new($false))
try {
    Set-Acl -LiteralPath $parent -AclObject $proposed
    $afterAcl = Get-Acl -LiteralPath $parent
    if ($afterAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $admins.Value -or
        -not $afterAcl.AreAccessRulesProtected) { throw 'Parent ACL verification failed.' }
} catch {
    $reason = $_.Exception.Message
    try { Set-Acl -LiteralPath $parent -AclObject $original }
    catch { throw "Parent ACL update failed ($reason), and rollback failed: $($_.Exception.Message); backup: $backup" }
    throw "Parent ACL update rolled back: $reason; backup: $backup"
}
Write-Output "PROTECTED: $parent; owner Administrators; inherited child grants retained; backup $backup"
