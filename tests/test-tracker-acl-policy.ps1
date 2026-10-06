$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-AdminCommon.ps1')
$base = Join-Path $repo '.runtime\tests\test-tracker-acl-policy'
$parent = Join-Path $base 'parent'
$root = Join-Path $parent 'Tracker'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$service = [Security.Principal.SecurityIdentifier]'S-1-5-21-1-2-3-1001'
$target = [Security.Principal.SecurityIdentifier]'S-1-5-21-1-2-3-1002'
$users = [Security.Principal.SecurityIdentifier]'S-1-5-11'

function New-TestAcl($owner, [bool]$protected) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($owner)
    $acl.SetAccessRuleProtection($protected, $false)
    return $acl
}
function Add-TestRight($acl, $sid, [string]$rights) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, $rights, 'Allow'))
}
$parentAcl = New-TestAcl $admins $true
Add-TestRight $parentAcl $users 'Modify'
$rootAcl = New-TestAcl $service $true
Add-TestRight $rootAcl $target 'ReadAndExecute'
function Get-Acl {
    param([string]$LiteralPath)
    if ($LiteralPath -ieq $parent) { return $parentAcl }
    if ($LiteralPath -ieq $root) { return $rootAcl }
    throw "Unexpected ACL lookup: $LiteralPath"
}
Assert-TrackerRootBoundary -TrackerRoot $root -TargetSid $target.Value -ServiceSid $service.Value | Out-Null

Add-TestRight $parentAcl $users 'DeleteSubdirectoriesAndFiles'
try {
    Assert-TrackerRootBoundary -TrackerRoot $root -TargetSid $target.Value -ServiceSid $service.Value | Out-Null
    throw 'Parent delete-child right was accepted.'
} catch { if ($_.Exception.Message -eq 'Parent delete-child right was accepted.') { throw } }
$parentAcl = New-TestAcl $admins $true
Add-TestRight $parentAcl $users 'Modify'
$rootAcl = New-TestAcl $service $false
try {
    Assert-TrackerRootBoundary -TrackerRoot $root -TargetSid $target.Value -ServiceSid $service.Value | Out-Null
    throw 'Inherited Tracker root ACL was accepted.'
} catch { if ($_.Exception.Message -eq 'Inherited Tracker root ACL was accepted.') { throw } }
$rootAcl = New-TestAcl $service $true
Add-TestRight $rootAcl $users 'Write'
try {
    Assert-TrackerRootBoundary -TrackerRoot $root -TargetSid $target.Value -ServiceSid $service.Value `
        -TrustAuthenticatedUsers $true | Out-Null
    throw 'Write access to Tracker root was accepted.'
} catch { if ($_.Exception.Message -eq 'Write access to Tracker root was accepted.') { throw } }
Write-Output 'Tracker ACL policy passed: parent Modify allowed; delete-child, inheritance and root write rejected'
