param(
    [Parameter(Mandatory)][ValidateSet('Setup', 'Agent', 'Cleanup')][string]$Mode,
    [string]$AgentAccount = 'VASIL\Vasil',
    [string]$ServiceAccount = 'VASIL\TaskFolderMcpSvc'
)

$ErrorActionPreference = 'Stop'
$parent = 'D:\TaskTrackers'
$results = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\.runtime\tests\A60-02'))
$names = @('_AIDEV60-probe-delete', '_AIDEV60-probe-rename')
$agentSid = ([Security.Principal.NTAccount]$AgentAccount).Translate([Security.Principal.SecurityIdentifier])
$serviceSid = ([Security.Principal.NTAccount]$ServiceAccount).Translate([Security.Principal.SecurityIdentifier])
$systemSid = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$adminSid = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Set-ProbeAcl([string]$path, $owner, [string]$serviceRights) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($owner)
    foreach ($entry in @(@($systemSid, 'FullControl'), @($adminSid, 'FullControl'), @($serviceSid, $serviceRights), @($agentSid, 'ReadAndExecute'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $entry[0], $entry[1], 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $path -AclObject $acl
}

function Describe-Acl([string]$path) {
    try {
        $acl = Get-Acl -LiteralPath $path
        return [ordered]@{
            path = $path; owner = $acl.Owner; inheritance_disabled = $acl.AreAccessRulesProtected
            rules = @($acl.Access | ForEach-Object {
                [ordered]@{ identity = $_.IdentityReference.Value; rights = $_.FileSystemRights.ToString(); type = $_.AccessControlType.ToString(); inherited = $_.IsInherited }
            })
        }
    } catch { return [ordered]@{ path = $path; error = $_.Exception.Message } }
}

if ($Mode -eq 'Setup') {
    if (-not $elevated) { throw 'Setup requires an elevated administrator token.' }
    if (Test-Path -LiteralPath $parent) { throw "Refusing to reuse existing parent: $parent" }
    New-Item -ItemType Directory -Path $parent | Out-Null
    Set-ProbeAcl $parent $adminSid 'ReadAndExecute'
    foreach ($name in $names) {
        $path = Join-Path $parent $name
        New-Item -ItemType Directory -Path $path | Out-Null
        Set-ProbeAcl $path $serviceSid 'FullControl'
    }
    $actual = Get-Acl -LiteralPath (Join-Path $parent $names[0])
    if ($actual.Owner -ne $ServiceAccount) { throw "Service ownership was not set: $($actual.Owner)" }
    [ordered]@{
        setup_identity = $identity.Name
        directories = @((Describe-Acl 'D:\'), (Describe-Acl $parent), (Describe-Acl (Join-Path $parent $names[0])), (Describe-Acl (Join-Path $parent $names[1])))
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $results 'safe-setup.json') -Encoding utf8
    Write-Output "READY: $parent"
    return
}

if ($Mode -eq 'Cleanup') {
    if (-not $elevated) { throw 'Cleanup requires an elevated administrator token.' }
    foreach ($name in @($names + '_AIDEV60-probe-renamed')) {
        $path = [IO.Path]::GetFullPath((Join-Path $parent $name))
        if ([IO.Path]::GetDirectoryName($path) -ne $parent) { throw "Unsafe cleanup path: $path" }
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe cleanup object: $path" }
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
    Write-Output "CLEAN: probe directories removed; protected parent retained at $parent"
    return
}

if ($identity.User -ne $agentSid -or $elevated) { throw "Agent mode requires unelevated $AgentAccount." }
$parentAcl = Get-Acl -LiteralPath $parent
if ($parentAcl.Owner -ne 'BUILTIN\Administrators' -or -not $parentAcl.AreAccessRulesProtected) { throw 'Unsafe test parent ACL.' }
foreach ($name in $names) {
    $path = Join-Path $parent $name
    $item = Get-Item -LiteralPath $path -Force
    $acl = Get-Acl -LiteralPath $path
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $acl.Owner -ne $ServiceAccount -or -not $acl.AreAccessRulesProtected) { throw "Unsafe test child: $path" }
}
$report = [ordered]@{
    identity = $identity.Name; sid = $identity.User.Value; elevated = $elevated
    integrity_sid = @(& whoami.exe /groups | Select-String 'S-1-16-\d+' -AllMatches | ForEach-Object { $_.Matches.Value })
    before = @((Describe-Acl 'D:\'), (Describe-Acl $parent), (Describe-Acl (Join-Path $parent $names[0])), (Describe-Acl (Join-Path $parent $names[1])))
    operations = @()
}
function Try-Operation([string]$name, [scriptblock]$operation) {
    try { & $operation; $script:report.operations += [ordered]@{ name = $name; result = 'succeeded' } }
    catch { $script:report.operations += [ordered]@{ name = $name; result = 'denied'; error = $_.Exception.Message } }
}
Try-Operation 'change-parent-acl' {
    $output = & icacls.exe $parent /grant "${AgentAccount}:(F)" 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
}
Try-Operation 'delete-Tracker' { [IO.Directory]::Delete((Join-Path $parent $names[0]), $true) }
Try-Operation 'rename-Tracker' { [IO.Directory]::Move((Join-Path $parent $names[1]), (Join-Path $parent '_AIDEV60-probe-renamed')) }
$report.after = @((Describe-Acl $parent), (Describe-Acl (Join-Path $parent $names[0])), (Describe-Acl (Join-Path $parent $names[1])))
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $results 'safe-agent.json') -Encoding utf8
$report.operations | Format-Table -AutoSize
if (@($report.operations | Where-Object result -eq 'succeeded').Count) { throw 'Protected parent failed ACL probe.' }
