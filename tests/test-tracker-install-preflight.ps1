$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $repo ('.runtime\tests\test-tracker-install-preflight\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$bootstrap = Join-Path $repo 'scripts\Bootstrap-TaskTracker.ps1'
$installer = Join-Path $repo 'scripts\Install-TaskTracker.ps1'
function Get-Service { [CmdletBinding()] param([string]$Name) return $null }
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
try { & $installer -ConfigPath $combined -ValidateOnly | Out-Null; throw 'Combined unsafe paths were accepted.' }
catch {
    $message = $_.Exception.Message
    if ($message -notmatch [regex]::Escape($fakeNode) -or
        $message -notmatch [regex]::Escape($personalRoot) -or
        $message -notmatch 'schemaVersion' -or
        $message -notmatch 'protectedRoot' -or
        $message -notmatch 'installRoot' -or
        $message -notmatch 'serviceName' -or
        $message -notmatch 'TASKS_INHERITED_ACL' -or
        $message -notmatch 'Protect-TrackerParent.ps1' -or
        $message -notmatch 'Protect-TrackerParent.ps1[^\r\n]+-Apply' -or
        $message -notmatch 'фактически:.*требуется:.*исправить:') { throw "Preflight missed a finding or remedy: $message" }
}
if ((Get-Acl -LiteralPath $personalRoot).GetSecurityDescriptorSddlForm('Access, Owner') -ne $parentAclBefore) {
    throw 'ValidateOnly changed the parent ACL.'
}
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
& $installer -ConfigPath $safe -ValidateOnly | Out-Null
try { & $installer -ConfigPath $unsafe -ValidateOnly | Out-Null; throw 'Unsafe parent was accepted.' }
catch { if ($_.Exception.Message -notmatch [regex]::Escape($personalRoot)) { throw } }
Write-Output 'Tracker installer preflight tests passed'
