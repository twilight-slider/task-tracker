$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ([Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this test under the ordinary agent token, without elevation.'
}
$root = 'C:\ProgramData\TaskFolderMcp\protected-test'
$task = Join-Path $root 'TFMTEST-1'
$snapshots = Join-Path $task 'snapshots'
$failures = [Collections.Generic.List[string]]::new()

try {
    [IO.File]::WriteAllText((Join-Path $root 'agent-write-probe.txt'), 'probe')
    $failures.Add('write')
} catch [UnauthorizedAccessException] { }

try {
    Move-Item -LiteralPath $task -Destination (Join-Path $root 'TFMTEST-1-renamed') -ErrorAction Stop
    $failures.Add('rename')
    Move-Item -LiteralPath (Join-Path $root 'TFMTEST-1-renamed') -Destination $task -ErrorAction Stop
} catch [UnauthorizedAccessException] { }

try {
    Remove-Item -LiteralPath $snapshots -Force -ErrorAction Stop
    $failures.Add('delete')
    New-Item -ItemType Directory -Path $snapshots -ErrorAction Stop | Out-Null
} catch [UnauthorizedAccessException] { }

try {
    $acl = Get-Acl -LiteralPath $root -ErrorAction Stop
    Set-Acl -LiteralPath $root -AclObject $acl -ErrorAction Stop
    $failures.Add('change ACL')
} catch [UnauthorizedAccessException] { }

if ($failures.Count) { throw "Agent could modify protected storage: $($failures -join ', ')" }
Write-Output 'Agent write, rename, delete and ACL changes were denied'
