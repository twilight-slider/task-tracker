$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-AdminCommon.ps1')
$root = Join-Path $repo '.runtime\tests\git-folder-env'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$envPath = Join-Path $root 'env.txt'
[IO.File]::WriteAllText($envPath, "GIT_FOLDER=$root`n", [Text.UTF8Encoding]::new($false))
if ((Read-GitFolderFromEnv -EnvPath $envPath) -ine $root) { throw 'GIT_FOLDER was not resolved.' }
foreach ($text in @('', "GIT_FOLDER=relative`n", "GIT_FOLDER=$root`nGIT_FOLDER=$root`n")) {
    [IO.File]::WriteAllText($envPath, $text, [Text.UTF8Encoding]::new($false))
    $failed = $false
    try { Read-GitFolderFromEnv -EnvPath $envPath | Out-Null } catch { $failed = $true }
    if (-not $failed) { throw 'Invalid GIT_FOLDER setting was accepted.' }
}
Write-Output 'GIT_FOLDER env checks passed'
