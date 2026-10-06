$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$base = Join-Path $repo ('.runtime\tests\stage-python-runtime\run-' + $PID)
New-Item -ItemType Directory -Path $base -Force | Out-Null
$stage = Join-Path $base 'runtime'
$script = Join-Path $repo 'scripts\Stage-PythonRuntime.ps1'
$oldFindLinks = $env:PIP_FIND_LINKS
$oldNoIndex = $env:PIP_NO_INDEX
try {
    if ($env:TRACKER_TEST_WHEEL_DIR) {
        $env:PIP_FIND_LINKS = $env:TRACKER_TEST_WHEEL_DIR
        $env:PIP_NO_INDEX = '1'
    }
    $result = & $script -RuntimeRoot $stage
} finally {
    $env:PIP_FIND_LINKS = $oldFindLinks
    $env:PIP_NO_INDEX = $oldNoIndex
}
if ($result.PythonVersion -ne '3.11' -or $result.PyYAMLVersion -ne '6.0.3') {
    throw 'Unexpected virtual environment Python/PyYAML version.'
}
$python = Join-Path $stage 'Scripts\python.exe'
$selectedBase = & $python -c 'import sys; print(sys.base_prefix)'
if ($LASTEXITCODE -ne 0 -or $selectedBase -ne (& py.exe -3.11 -c 'import sys; print(sys.prefix)')) {
    throw 'Runtime was not created from launcher-selected Python 3.11.'
}
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

$installed = (& py.exe -0p) -join "`n"
if ($installed -notmatch '(?m)^\s*-V:3\.12(?:\s|$)') {
    $missingVersionFile = Join-Path $base 'missing-python-version.txt'
    [IO.File]::WriteAllText($missingVersionFile, "python3.12`n")
    $missingRuntime = Join-Path $base 'missing-version-runtime'
    $rejected = $false
    try { & $script -RuntimeRoot $missingRuntime -VersionFile $missingVersionFile | Out-Null }
    catch { $rejected = $_.Exception.Message -like '*Python 3.12 is unavailable*Install it or change*Python 3.11 is recommended*' }
    if (-not $rejected -or (Test-Path -LiteralPath $missingRuntime)) {
        throw 'Missing configured Python version must stop before runtime creation.'
    }
}

$defaultPython = Get-Command python.exe -CommandType Application | Select-Object -First 1 -ExpandProperty Source
$defaultVersion = & $defaultPython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))'
if ($defaultVersion -ne '3.11') {
    $invalid = Join-Path $base 'invalid-runtime'
    $rejected = $false
    try { & $script -RuntimeRoot $invalid -PythonExe $defaultPython | Out-Null }
    catch { $rejected = $_.Exception.Message -like '*Python 3.11 is required*' }
    if (-not $rejected) { throw 'Wrong Python version was not rejected.' }
    if (Test-Path -LiteralPath $invalid) { throw 'Wrong Python version created a runtime directory.' }
}

Write-Output 'Protected venv staging, pip dependency install, worker import and strict-version refusal passed'
