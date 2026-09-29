param(
    [Parameter(Mandatory)][ValidateSet('Setup', 'Agent')][string]$Mode,
    [string]$TestRoot = (Join-Path $PSScriptRoot '..\.runtime\tests\A60-02\unsafe-parent'),
    [string]$AgentAccount = 'VASIL\Vasil',
    [string]$ServiceAccount = 'VASIL\TaskFolderMcpSvc'
)

$ErrorActionPreference = 'Stop'
$base = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\.runtime\tests\A60-02'))
$root = [IO.Path]::GetFullPath($TestRoot)
if (-not $root.StartsWith($base + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "TestRoot must be below $base"
}
$agentSid = ([Security.Principal.NTAccount]$AgentAccount).Translate([Security.Principal.SecurityIdentifier])
$serviceSid = ([Security.Principal.NTAccount]$ServiceAccount).Translate([Security.Principal.SecurityIdentifier])
$systemSid = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$adminSid = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Add-Rule($acl, $sid, $rights) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $sid, $rights, 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
}

function Describe-Acl([string]$path) {
    try {
        $acl = Get-Acl -LiteralPath $path
        return [ordered]@{
            path = $path
            owner = $acl.Owner
            inheritance_disabled = $acl.AreAccessRulesProtected
            rules = @($acl.Access | ForEach-Object {
                [ordered]@{ identity = $_.IdentityReference.Value; rights = $_.FileSystemRights.ToString(); type = $_.AccessControlType.ToString(); inherited = $_.IsInherited }
            })
        }
    } catch { return [ordered]@{ path = $path; error = $_.Exception.Message } }
}

if ($Mode -eq 'Setup') {
    if (-not $elevated) { throw 'Setup requires an elevated administrator token.' }
    if (Test-Path -LiteralPath $root) { throw "Refusing to reuse test directory: $root" }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $parentAcl = [Security.AccessControl.DirectorySecurity]::new()
    $parentAcl.SetAccessRuleProtection($true, $false)
    $parentAcl.SetOwner($agentSid)
    foreach ($sid in @($systemSid, $adminSid, $serviceSid)) { Add-Rule $parentAcl $sid 'FullControl' }
    Add-Rule $parentAcl $agentSid 'ReadAndExecute'
    Set-Acl -LiteralPath $root -AclObject $parentAcl
    foreach ($name in @('Tracker-delete', 'Tracker-rename')) {
        $path = Join-Path $root $name
        New-Item -ItemType Directory -Path $path | Out-Null
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($serviceSid)
        foreach ($sid in @($systemSid, $adminSid, $serviceSid)) { Add-Rule $acl $sid 'FullControl' }
        Add-Rule $acl $agentSid 'ReadAndExecute'
        Set-Acl -LiteralPath $path -AclObject $acl
    }
    $actual = Get-Acl -LiteralPath (Join-Path $root 'Tracker-delete')
    if ($actual.Owner -ne $ServiceAccount) { throw "Service ownership was not set: $($actual.Owner)" }
    [ordered]@{
        setup_identity = $identity.Name
        setup_elevated = $elevated
        directories = @((Describe-Acl $root), (Describe-Acl (Join-Path $root 'Tracker-delete')), (Describe-Acl (Join-Path $root 'Tracker-rename')))
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $base 'setup-result.json') -Encoding utf8
    Write-Output "READY: $root"
    return
}

if ($identity.User -ne $agentSid -or $elevated) { throw "Agent mode requires unelevated $AgentAccount." }
$parentBefore = Get-Acl -LiteralPath $root
if ($parentBefore.Owner -ne $AgentAccount) { throw "Unexpected parent owner: $($parentBefore.Owner)" }
foreach ($name in @('Tracker-delete', 'Tracker-rename')) {
    $item = Get-Item -LiteralPath (Join-Path $root $name) -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe test target: $name" }
    $childAcl = Get-Acl -LiteralPath $item.FullName
    if ($childAcl.Owner -ne $ServiceAccount -or -not $childAcl.AreAccessRulesProtected) { throw "Unexpected child ACL: $name" }
}
$report = [ordered]@{
    identity = $identity.Name
    sid = $identity.User.Value
    elevated = $elevated
    integrity_sid = @($identity.Groups | Where-Object { $_.Value -like 'S-1-16-*' } | ForEach-Object Value)
    before = @((Describe-Acl $root), (Describe-Acl (Join-Path $root 'Tracker-delete')), (Describe-Acl (Join-Path $root 'Tracker-rename')))
    operations = @()
}
function Try-Operation([string]$name, [scriptblock]$operation) {
    try { & $operation; $script:report.operations += [ordered]@{ name = $name; result = 'succeeded' } }
    catch { $script:report.operations += [ordered]@{ name = $name; result = 'denied'; error = $_.Exception.Message } }
}
Try-Operation 'change-parent-acl' {
    $acl = Get-Acl -LiteralPath $root
    Add-Rule $acl $agentSid 'FullControl'
    Set-Acl -LiteralPath $root -AclObject $acl
}
Try-Operation 'delete-Tracker' { [IO.Directory]::Delete((Join-Path $root 'Tracker-delete'), $true) }
Try-Operation 'rename-Tracker' { [IO.Directory]::Move((Join-Path $root 'Tracker-rename'), (Join-Path $root 'Tracker-renamed')) }
$report.after = @((Describe-Acl $root), (Describe-Acl (Join-Path $root 'Tracker-delete')), (Describe-Acl (Join-Path $root 'Tracker-rename')), (Describe-Acl (Join-Path $root 'Tracker-renamed')))
$reportPath = Join-Path $base 'unsafe-parent-result.json'
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding utf8
Write-Output "RESULT: $reportPath"
$report.operations | Format-Table -AutoSize
