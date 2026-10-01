$ErrorActionPreference = 'Stop'
$root = Join-Path (Split-Path -Parent $PSScriptRoot) ('.runtime\tests\test-tracker-bootstrap\run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$envPath = Join-Path $root 'env.txt'
$configPath = Join-Path $root 'bootstrap.json'
$tracker = Join-Path $root 'Tracker One'
$bootstrap = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\Bootstrap-TaskTracker.ps1'
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=$tracker`n", [Text.UTF8Encoding]::new($false))
& $bootstrap -EnvPath $envPath -ConfigPath $configPath | Out-Null
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.trackerRoot -ne [IO.Path]::GetFullPath($tracker) -or
    $config.tasksRoot -ne (Join-Path $tracker 'tasks') -or
    $config.protectedRoot -ne (Join-Path $tracker '.protected') -or
    $config.installRoot -ne (Join-Path $tracker '.protected\bin')) { throw 'Bootstrap did not derive paths from TRACKER_FOLDER.' }
$before = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
& $bootstrap -EnvPath $envPath -ConfigPath $configPath | Out-Null
if ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $before) { throw 'Repeated bootstrap changed the request.' }
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=$(Join-Path $root 'Other')`n", [Text.UTF8Encoding]::new($false))
try { & $bootstrap -EnvPath $envPath -ConfigPath $configPath | Out-Null; throw 'Changed env root was accepted.' }
catch { if ($_.Exception.Message -eq 'Changed env root was accepted.') { throw } }
[IO.File]::WriteAllText($envPath, "TRACKER_FOLDER=relative\path`n", [Text.UTF8Encoding]::new($false))
try { & $bootstrap -EnvPath $envPath -ConfigPath (Join-Path $root 'invalid.json') | Out-Null; throw 'Relative root was accepted.' }
catch { if ($_.Exception.Message -eq 'Relative root was accepted.') { throw } }
Write-Output 'Tracker bootstrap tests passed'
