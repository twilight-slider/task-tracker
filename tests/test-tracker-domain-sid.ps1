$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $repo ('.runtime\tests\test-tracker-domain-sid\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$node = Join-Path $root 'node.exe'
[IO.File]::WriteAllText($node, 'fixture', [Text.UTF8Encoding]::new($false))
$acl = Get-Acl -LiteralPath $node
$acl.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm('Access') + '(A;;GA;;;S-1-5-32)', 'Access')
Set-Acl -LiteralPath $node -AclObject $acl
$envPath = Join-Path $root 'env.txt'
$configPath = Join-Path $root 'request.json'
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=$(Join-Path $root 'Tracker')`n", [Text.UTF8Encoding]::new($false))
& (Join-Path $repo 'scripts\Bootstrap-TaskTracker.ps1') -EnvPath $envPath -ConfigPath $configPath | Out-Null
$request = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$request.nodePath = $node
[IO.File]::WriteAllText($configPath, (($request | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
function Get-Service { [CmdletBinding()] param([string]$Name) return $null }
try { & (Join-Path $repo 'scripts\Install-TaskTracker.ps1') -ConfigPath $configPath -ValidateOnly | Out-Null; throw 'User-owned test executable was accepted.' }
catch {
    $message = $_.Exception.Message
    if ($message -notmatch 'EXECUTABLE_OWNER' -or $message -match 'S-1-5-32 has FullControl') { throw }
}
Write-Output 'Builtin domain SID is not treated as an effective grant'
