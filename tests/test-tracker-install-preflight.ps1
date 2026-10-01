$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $repo ('.runtime\tests\test-tracker-install-preflight\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$bootstrap = Join-Path $repo 'scripts\Bootstrap-TaskTracker.ps1'
$installer = Join-Path $repo 'scripts\Install-TaskTracker.ps1'
function Get-Service { [CmdletBinding()] param([string]$Name) if ($global:fakeInstalled) { return [pscustomobject]@{ Status = 'Running' } } return $null }
function New-Request([string]$name, [string]$trackerRoot) {
    $envPath = Join-Path $root "$name.env.txt"
    $configPath = Join-Path $root "$name.json"
    [IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=$trackerRoot`n", [Text.UTF8Encoding]::new($false))
    & $bootstrap -EnvPath $envPath -ConfigPath $configPath | Out-Null
    return $configPath
}
$safeRoot = Join-Path ([IO.Path]::GetPathRoot($repo)) "AIDEV60-preflight-$PID\Tracker"
$safe = New-Request 'safe' $safeRoot
$aiRoot = Join-Path $root 'AI'
$personalRoot = Join-Path $aiRoot 'user'
$unsafeRoot = Join-Path $personalRoot 'TaskTracker'
New-Item -ItemType Directory -Path $unsafeRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $unsafeRoot 'tasks') -Force | Out-Null
foreach ($target in @($personalRoot, $unsafeRoot)) {
    $preview = & (Join-Path $repo 'scripts\Protect-TrackerParent.ps1') -TrackerRoot $target
    if ($preview -notmatch '^READY:') { throw "Parent ACL preview did not use $target" }
}
$tasksPreview = & (Join-Path $repo 'scripts\Protect-TrackerTasks.ps1') -TrackerRoot $unsafeRoot
if ($tasksPreview -notmatch '^READY:' -or (Get-Acl -LiteralPath (Join-Path $unsafeRoot 'tasks')).AreAccessRulesProtected) {
    throw 'Existing tasks ACL preview changed or failed to identify inheritance.'
}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try {
        & (Join-Path $repo 'scripts\Protect-TrackerTasks.ps1') -TrackerRoot $unsafeRoot -Apply | Out-Null
        throw 'Tasks ACL Apply accepted a non-administrator.'
    } catch {
        if ($_.Exception.Message -notmatch 'Apply requires an elevated PowerShell window') { throw }
    }
}
$unsafe = New-Request 'unsafe' $unsafeRoot
$untrusted = New-Request 'untrusted-node' $safeRoot
$fakeNode = Join-Path $root 'node.exe'
[IO.File]::WriteAllText($fakeNode, 'fixture', [Text.UTF8Encoding]::new($false))
$request = Get-Content -LiteralPath $untrusted -Raw | ConvertFrom-Json
$request.nodePath = $fakeNode
[IO.File]::WriteAllText($untrusted, (($request | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
$combined = New-Request 'combined' $unsafeRoot
$request = Get-Content -LiteralPath $combined -Raw | ConvertFrom-Json
$request.schemaVersion = 1
$request.nodePath = $fakeNode
foreach ($field in @('protectedRoot', 'installRoot', 'serviceName')) { $request.PSObject.Properties.Remove($field) }
[IO.File]::WriteAllText($combined, (($request | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
$parentAclBefore = (Get-Acl -LiteralPath $personalRoot).GetSecurityDescriptorSddlForm('Access, Owner')
$profile = [Environment]::GetFolderPath('UserProfile').TrimEnd('\')
$insideProfile = $personalRoot.StartsWith("$profile\", [StringComparison]::OrdinalIgnoreCase)
try { & $installer -ConfigPath $combined -ValidateOnly | Out-Null; throw 'Combined unsafe paths were accepted.' }
catch {
    $message = $_.Exception.Message
    $parentRemedyPresent = if ($insideProfile) {
        $message -match 'Choose a dedicated Tracker location; do not change this shared/system/user directory automatically' -and
        $message -notmatch 'Only if this parent is dedicated:'
    } else {
        $message -match 'Install-TaskTracker.ps1[^\r\n]+-PrepareAcl -ApplyAcl'
    }
    if ($message -notmatch [regex]::Escape($fakeNode) -or
        $message -notmatch [regex]::Escape($personalRoot) -or
        $message -notmatch 'schemaVersion' -or
        $message -notmatch 'protectedRoot' -or
        $message -notmatch 'installRoot' -or
        $message -notmatch 'serviceName' -or
        $message -notmatch 'TASKS_INHERITED_ACL' -or
        $message -notmatch 'Install-TaskTracker.ps1[^\r\n]+-PrepareAcl -ApplyAcl' -or
        -not $parentRemedyPresent -or
        $message -notmatch 'фактически:.*требуется:.*исправить:') { throw "Preflight missed a finding or remedy: $message" }
}
if ((Get-Acl -LiteralPath $personalRoot).GetSecurityDescriptorSddlForm('Access, Owner') -ne $parentAclBefore) {
    throw 'ValidateOnly changed the parent ACL.'
}
try { & $installer -ConfigPath $combined -PrepareAcl | Out-Null; throw 'ACL plan accepted an invalid bootstrap request.' }
catch { if ($_.Exception.Message -notmatch 'schemaVersion') { throw } }
try { & $installer -ConfigPath $untrusted -ValidateOnly | Out-Null; throw 'User-owned Node path was accepted.' }
catch { if ($_.Exception.Message -notmatch [regex]::Escape($fakeNode)) { throw } }
$fakePwsh = Join-Path $root 'pwsh.exe'
[IO.File]::WriteAllText($fakePwsh, 'fixture', [Text.UTF8Encoding]::new($false))
$oldPath = $env:PATH
try {
    $env:PATH = "$root;$oldPath"
    try { & $installer -ConfigPath $safe -ValidateOnly | Out-Null; throw 'User-owned PowerShell path was accepted.' }
    catch { if ($_.Exception.Message -notmatch [regex]::Escape($fakePwsh)) { throw } }
} finally { $env:PATH = $oldPath }
try { & $installer -ConfigPath $unsafe -ValidateOnly | Out-Null; throw 'Unsafe parent was accepted.' }
catch { if ($_.Exception.Message -notmatch [regex]::Escape($personalRoot)) { throw } }
$aclBefore = (Get-Acl -LiteralPath $personalRoot).GetSecurityDescriptorSddlForm('Access, Owner')
if ($insideProfile) {
    try { & $installer -ConfigPath $unsafe -PrepareAcl | Out-Null; throw 'ACL plan accepted a Tracker inside the user profile.' }
    catch { if ($_.Exception.Message -notmatch 'do not change this shared/system/user directory automatically') { throw } }
} else {
    $preview = @(& $installer -ConfigPath $unsafe -PrepareAcl)
    if (-not @($preview | Where-Object { $_ -match '^ACL PREVIEW READY:' }).Count -or
        -not @($preview | Where-Object { $_ -match [regex]::Escape((Join-Path $unsafeRoot 'tasks')) }).Count) {
        throw "Installer did not construct the ACL plan from bootstrap JSON: $preview"
    }
}
if ((Get-Acl -LiteralPath $personalRoot).GetSecurityDescriptorSddlForm('Access, Owner') -ne $aclBefore) {
    throw 'ACL preview changed the parent.'
}
try { & $installer -ConfigPath $unsafe -ApplyAcl | Out-Null; throw 'ApplyAcl without PrepareAcl was accepted.' }
catch { if ($_.Exception.Message -notmatch 'ApplyAcl requires PrepareAcl') { throw } }
try { & $installer -ConfigPath $unsafe -ApplyImport | Out-Null; throw 'ApplyImport without ImportExisting was accepted.' }
catch { if ($_.Exception.Message -notmatch 'ApplyImport requires ImportExisting') { throw } }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try { & $installer -ConfigPath $unsafe -PrepareAcl -ApplyAcl | Out-Null; throw 'ACL Apply accepted a non-administrator.' }
    catch { if ($_.Exception.Message -notmatch 'elevated PowerShell') { throw } }
    $global:fakeInstalled = $true
    try {
        try { & $installer -ConfigPath $unsafe -ValidateOnly | Out-Null; throw 'Installed service validation accepted a non-administrator.' }
        catch { if ($_.Exception.Message -notmatch 'Installed Tracker checks require elevated PowerShell') { throw } }
    } finally { $global:fakeInstalled = $false }
}
Write-Output 'Tracker installer preflight tests passed'
