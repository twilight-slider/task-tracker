$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $repo ('.runtime\tests\test-protect-tracker-parent\run-' + [guid]::NewGuid().ToString('N'))
$parent = Join-Path $root 'Parent'
$tracker = Join-Path $parent 'Tracker'
New-Item -ItemType Directory -Path $tracker -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $parent 'sibling.txt'), 'fixture', [Text.UTF8Encoding]::new($false))
$acl = Get-Acl -LiteralPath $parent
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$sddl = $acl.GetSecurityDescriptorSddlForm('Access') + "(A;OICI;GRGWGX;;;$($sid.Value))(A;OICI;GA;;;S-1-5-32-545)"
$acl.SetSecurityDescriptorSddlForm($sddl, 'Access')
Set-Acl -LiteralPath $parent -AclObject $acl
$before = (Get-Acl -LiteralPath $parent).GetSecurityDescriptorSddlForm('Access, Owner')
$preview = & (Join-Path $repo 'scripts\Protect-TrackerParent.ps1') -TrackerRoot $tracker
if ($preview -notmatch '^READY:' -or $preview -notmatch '2 immediate children') { throw "Unexpected parent preview: $preview" }
if ((Get-Acl -LiteralPath $parent).GetSecurityDescriptorSddlForm('Access, Owner') -ne $before) {
    throw 'Parent preview changed its ACL.'
}
$envPath = Join-Path $root 'env.txt'
$configPath = Join-Path $root 'request.json'
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=$tracker`n", [Text.UTF8Encoding]::new($false))
& (Join-Path $repo 'scripts\Bootstrap-TaskTracker.ps1') -EnvPath $envPath -ConfigPath $configPath | Out-Null
function Get-Service { [CmdletBinding()] param([string]$Name) return $null }
try { & (Join-Path $repo 'scripts\Install-TaskTracker.ps1') -ConfigPath $configPath -ValidateOnly | Out-Null; throw 'GenericAll parent ACE was accepted.' }
catch {
    if ($_.Exception.Message -notmatch 'PARENT_UNSAFE_GRANT' -or $_.Exception.Message -notmatch 'S-1-5-32-545') { throw }
}
Write-Output 'Tracker parent preview with generic ACE passed'
