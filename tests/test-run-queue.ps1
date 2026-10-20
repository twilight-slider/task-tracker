$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path $repo ('.runtime\tests\run-queue\run-' + $PID)
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Root Python runtime is required.' }
$worker = Join-Path $testRoot 'worker.py'
@'
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"ok": True, "data": request["method"]}), flush=True)
'@ | Set-Content -LiteralPath $worker -Encoding UTF8
$csc = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe = Join-Path $testRoot 'RunQueueProbe.exe'
& $csc /nologo /target:exe /main:RunQueueProbe "/out:$exe" `
    /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll `
    (Join-Path $repo 'src\ServiceHost.cs') (Join-Path $PSScriptRoot 'RunQueueProbe.cs')
if ($LASTEXITCODE -ne 0) { throw 'Run queue probe did not compile.' }
& $exe (Join-Path $testRoot 'runs.json') $python $worker (Join-Path $testRoot 'service.json')
if ($LASTEXITCODE -ne 0) { throw "Run queue probe failed with exit code $LASTEXITCODE." }
