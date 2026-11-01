param(
    [Parameter(Mandatory)][string]$ProbeScript,
    [Parameter(Mandatory)][string]$CodexExecutable,
    [Parameter(Mandatory)][string]$TestRoot,
    [Parameter(Mandatory)][string]$CompletionPath,
    [string]$AppContainerName,
    [switch]$RealTurn
)

$ErrorActionPreference = 'Stop'
$env:TEMP = $TestRoot
$env:TMP = $TestRoot
$code = 1
try {
    & $ProbeScript -CodexExecutable $CodexExecutable -TestRoot $TestRoot `
        -RealTurn:$RealTurn -AppContainerName $AppContainerName
    $code = 0
} catch {
    [Console]::Error.WriteLine($_.ToString())
} finally {
    [IO.File]::WriteAllText($CompletionPath, [string]$code)
}
exit $code
