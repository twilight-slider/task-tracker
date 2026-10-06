param([Parameter(Mandatory)][string]$TargetUser, [switch]$TrustAuthenticatedUsers)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1')
Assert-TrackerAdministrator
$settings = Get-TargetTrackerSettings -TargetUser $TargetUser
$installedServiceSid = Get-InstalledTrackerServiceSid -TargetSid $settings.TargetSid
$root = $settings.TrackerRoot
$parent = [IO.Path]::GetDirectoryName($root)
if (-not $parent -or $parent -eq [IO.Path]::GetPathRoot($root)) {
    throw 'Choose a dedicated Tracker path below an administrator-owned parent, not directly on a volume root.'
}
$parent = Assert-PlainDirectory $parent
foreach ($shared in @($settings.Profile, [Environment]::GetFolderPath('Windows'),
        [Environment]::GetFolderPath('ProgramFiles'), [Environment]::GetFolderPath('CommonApplicationData'))) {
    if ($shared -and ($root -ieq $shared -or $root.StartsWith("$shared\", [StringComparison]::OrdinalIgnoreCase))) {
        throw "TRACKER_FOLDER is inside a user/system directory. Choose a dedicated administrator-owned path: $root"
    }
}
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$target = [Security.Principal.SecurityIdentifier]$settings.TargetSid
$parentAcl = Get-Acl -LiteralPath $parent
$parentOwner = $parentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value
if ($parentOwner -notin @($admins.Value, $system.Value)) {
    throw "Tracker parent is not administrator-owned: $parent (owner $parentOwner). Choose or prepare a dedicated parent; do not change shared parent ACL automatically."
}
$parentDanger = [Security.AccessControl.FileSystemRights]'DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
$rootDanger = [Security.AccessControl.FileSystemRights]'CreateFiles, CreateDirectories, WriteData, AppendData, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
foreach ($rule in $parentAcl.Access) {
    if ($rule.AccessControlType -ne 'Allow' -or $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if (-not (Test-TrustedTrackerBoundarySid -Sid $sid -ServiceSid $installedServiceSid `
        -TrustAuthenticatedUsers ([bool]$TrustAuthenticatedUsers)) -and ($rule.FileSystemRights -band $parentDanger)) {
        throw "Tracker parent permits untrusted delete/ACL rights: $parent ($sid). Prepare a dedicated protected parent first."
    }
}
function Set-PreparedAcl([string]$Path, [bool]$AgentRead) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($admins)
    foreach ($entry in @(@($admins, 'FullControl'), @($system, 'FullControl'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($entry[0], $entry[1],
            'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    if ($AgentRead) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($target, 'ReadAndExecute',
            'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
if (Test-Path -LiteralPath $root) {
    $root = Assert-PlainDirectory $root
    $rootAcl = Get-Acl -LiteralPath $root
    $rootOwner = $rootAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($rootOwner -eq $settings.TargetSid) {
        throw "Existing Tracker is owned by TargetUser: $root. Preserve its owner; choose an administrator/service-owned Tracker or arrange an explicit owner decision."
    }
    foreach ($rule in $rootAcl.Access) {
        if ($rule.AccessControlType -ne 'Allow' -or $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -eq $settings.TargetSid -and ($rule.FileSystemRights -band $rootDanger)) {
            throw "TargetUser can delete or alter ACL beneath existing Tracker: $root. Repair its ACL explicitly without changing owner."
        }
    }
    Write-Output "Existing Tracker owner preserved: $root ($rootOwner)"
} else {
    New-Item -ItemType Directory -Path $root | Out-Null
    Set-PreparedAcl -Path $root -AgentRead $true
}
$protected = Join-Path $root '.protected'
if (-not (Test-Path -LiteralPath $protected)) {
    New-Item -ItemType Directory -Path $protected | Out-Null
    Set-PreparedAcl -Path $protected -AgentRead $false
}
$root = Assert-TrackerRootBoundary -TrackerRoot $root -TargetSid $settings.TargetSid `
    -ServiceSid $installedServiceSid -TrustAuthenticatedUsers ([bool]$TrustAuthenticatedUsers)
$serviceSid = $null
$installed = Join-Path $protected 'service.json'
if (Test-Path -LiteralPath $installed -PathType Leaf) {
    try { $serviceSid = [string]((Read-TrackerUtf8File $installed | ConvertFrom-Json).serviceAccountSid) } catch { }
}
if ($serviceSid -and $installedServiceSid -and $serviceSid -cne $installedServiceSid) {
    throw 'Installed service account SID differs from protected service.json.'
}
Assert-TrackerProtectedArea -TrackerRoot $root -TargetSid $settings.TargetSid -ServiceSid $installedServiceSid | Out-Null
Write-Output "Tracker root ready: $root; protected area: $protected; existing owner unchanged."
