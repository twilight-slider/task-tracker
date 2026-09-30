$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $repo ('.runtime\tests\test-tracker-install-preflight\run-' + $PID)
New-Item -ItemType Directory -Path $root -Force | Out-Null
$bootstrap = Join-Path $repo 'scripts\Bootstrap-TaskTracker.ps1'
$installer = Join-Path $repo 'scripts\Install-TaskTracker.ps1'
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
try { & $installer -ConfigPath $untrusted -ValidateOnly | Out-Null; throw 'User-owned Node path was accepted.' }
catch { if ($_.Exception.Message -notlike 'Untrusted owner of executable path:*') { throw } }
$rid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value.Split('-')[-1]
if (-not (Get-Service -Name "TaskTracker-$rid" -ErrorAction SilentlyContinue)) {
    & $installer -ConfigPath $safe -ValidateOnly | Out-Null
    try { & $installer -ConfigPath $unsafe -ValidateOnly | Out-Null; throw 'Unsafe parent was accepted.' }
    catch { if ($_.Exception.Message -notlike 'Untrusted owner of Tracker parent:*') { throw } }
} else { Write-Output 'Existing Tracker service: skipped fresh-install root checks' }
Write-Output 'Tracker installer preflight tests passed'
