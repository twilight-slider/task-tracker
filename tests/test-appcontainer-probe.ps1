param([string]$CodexExecutable, [string]$ProtectedFile, [string]$TestRoot, [switch]$RealTurn, [string]$AppContainerName, [switch]$SharedHomeAcl, [switch]$CreateOnlyAcl)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$allowed = ([IO.Path]::GetFullPath((Join-Path $repo '.runtime\tests'))).TrimEnd('\') + '\'
$testRoot = if ($TestRoot) { [IO.Path]::GetFullPath($TestRoot) } else {
    Join-Path $repo ('.runtime\tests\appcontainer-probe\run-' + $PID)
}
if (-not $testRoot.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'TestRoot must be inside the repository .runtime/tests directory.'
}
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$env:TEMP = $testRoot
$env:TMP = $testRoot
$source = Join-Path $PSScriptRoot 'AppContainerProbe.cs'
$exe = Join-Path $testRoot 'AppContainerProbe.exe'
$csc = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe /reference:System.Web.Extensions.dll "/out:$exe" $source
if ($LASTEXITCODE -ne 0) { throw 'AppContainer probe did not compile.' }
if ($SharedHomeAcl -or $CreateOnlyAcl) {
    if (($SharedHomeAcl -and $CreateOnlyAcl) -or $CodexExecutable -or $ProtectedFile -or $RealTurn) {
        throw 'Select one ACL probe without Codex or protected-file arguments.'
    }
    if ($SharedHomeAcl) { & $exe shared-acl $testRoot }
    else { & $exe create-only-acl $testRoot }
} elseif ($CodexExecutable) {
    if (-not (Test-Path -LiteralPath $CodexExecutable -PathType Leaf)) { throw 'Codex executable not found.' }
    $codexCopy = Join-Path $testRoot 'codex.exe'
    Copy-Item -LiteralPath $CodexExecutable -Destination $codexCopy
    if (-not $ProtectedFile) {
        $ProtectedFile = Join-Path (Split-Path -Parent $testRoot) 'protected-marker.txt'
        Set-Content -LiteralPath $ProtectedFile -Value 'AppContainer must not read this marker.'
    }
    if ($RealTurn -and $AppContainerName) { & $exe $codexCopy $ProtectedFile turn $AppContainerName }
    elseif ($RealTurn) { & $exe $codexCopy $ProtectedFile turn }
    else { & $exe $codexCopy $ProtectedFile }
} else {
    if ($ProtectedFile) { throw 'ProtectedFile requires CodexExecutable.' }
    & $exe
}
if ($LASTEXITCODE -ne 0) { throw "AppContainer probe failed with exit code $LASTEXITCODE." }
if ($SharedHomeAcl) { Write-Output 'Shared-home ACL counterexample probe passed' }
elseif ($CreateOnlyAcl) { Write-Output 'Create-only ACL experiment completed' }
else { Write-Output 'AppContainer process-isolation probe passed' }
