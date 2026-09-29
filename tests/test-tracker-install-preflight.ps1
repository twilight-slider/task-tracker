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
$safe = New-Request 'safe' "D:\AIDEV60-preflight-$PID\Vasil"
$unsafeParent = Join-Path $root 'unsafe-parent'
New-Item -ItemType Directory -Path $unsafeParent | Out-Null
$unsafe = New-Request 'unsafe' (Join-Path $unsafeParent 'Tracker')
& $installer -ConfigPath $safe -ValidateOnly | Out-Null
try { & $installer -ConfigPath $unsafe -ValidateOnly | Out-Null; throw 'Unsafe parent was accepted.' }
catch { if ($_.Exception.Message -eq 'Unsafe parent was accepted.') { throw } }
Write-Output 'Tracker installer preflight tests passed'
