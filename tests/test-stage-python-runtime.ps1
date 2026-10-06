$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$base = Join-Path $repo ('.runtime\tests\stage-python-runtime\run-' + $PID)
New-Item -ItemType Directory -Path $base -Force | Out-Null
$stage = Join-Path $base 'runtime'
$script = Join-Path $repo 'scripts\Stage-PythonRuntime.ps1'
$result = & $script -RuntimeRoot $stage
if ($result.PythonVersion -ne '3.11.9' -or $result.PyYAMLVersion -ne '6.0.3') {
    throw 'Unexpected staged runtime version.'
}
$python = Join-Path $stage 'python.exe'
$probe = & $python -I -c 'import yaml,sys; print(yaml.__version__,sys.flags.isolated)' 2>&1
if ($LASTEXITCODE -ne 0 -or $probe -ne '6.0.3 1') { throw "Staged runtime import failed: $probe" }
$bin = Join-Path $base 'bin'
New-Item -ItemType Directory -Path $bin | Out-Null
foreach ($module in @('folder_worker.py', 'task_folder.py', 'result_snapshot_store.py')) {
    Copy-Item -LiteralPath (Join-Path $repo "src\$module") -Destination $bin
}
$config = Join-Path $base 'service.json'
$tracker = Join-Path $base 'Tracker'
@{
    schemaVersion = 1
    trackerRoot = $tracker
    tasksRoot = (Join-Path $tracker 'tasks')
    protectedRoot = (Join-Path $tracker '.protected')
} | ConvertTo-Json | Set-Content -LiteralPath $config -Encoding utf8
$reply = '{"method":"get_tasks_folder","arguments":{}}' | & $python -I (Join-Path $bin 'folder_worker.py') $config
if ($LASTEXITCODE -ne 0 -or -not (($reply | ConvertFrom-Json).ok)) { throw "Protected-style worker import failed: $reply" }

$tampered = Join-Path $base 'tampered-packages'
New-Item -ItemType Directory -Path $tampered | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'vendor\python-3.11.9-embed-amd64.zip') -Destination $tampered
$wheel = Join-Path $tampered 'pyyaml-6.0.3-cp311-cp311-win_amd64.whl'
Copy-Item -LiteralPath (Join-Path $repo 'vendor\pyyaml-6.0.3-cp311-cp311-win_amd64.whl') -Destination $wheel
[IO.File]::AppendAllText($wheel, 'tampered')
$rejected = $false
try { & $script -RuntimeRoot (Join-Path $base 'invalid-runtime') -PackageRoot $tampered | Out-Null }
catch { $rejected = $_.Exception.Message -like '*hash mismatch*' }
if (-not $rejected) { throw 'Tampered wheel was not rejected.' }
if (Test-Path -LiteralPath (Join-Path $base 'invalid-runtime')) { throw 'Tampered package created a runtime directory.' }

Write-Output 'Pinned embedded Python/PyYAML staging and hash rejection passed'
