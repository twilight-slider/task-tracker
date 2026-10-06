$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$files = @(
    'TaskTracker-AdminCommon.ps1', 'Prepare-TrackerRoot.ps1', 'Bootstrap-TaskTrackerAdmin.ps1',
    'TaskTracker-AdminWrapper.ps1', 'Rebuild-TaskTracker.ps1', 'Sync-TaskTrackerToken.ps1',
    'TaskTracker-TokenCore.ps1', 'TaskTracker-InstallLayout.ps1', 'TaskTracker-Marketplace.ps1',
    'Update-TaskTrackerMarketplace.ps1', 'Start-CodexWithTrackerToken.ps1', 'Install-TaskTrackerV3.ps1'
)
foreach ($name in $files) {
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo "scripts\$name"), [ref]$null, [ref]$errors) | Out-Null
    if ($errors) { throw "PowerShell parser rejected $name : $errors" }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $user = $identity.Name
    foreach ($name in @('Prepare-TrackerRoot.ps1', 'Bootstrap-TaskTrackerAdmin.ps1')) {
        $rejected = $false
        try { & (Join-Path $repo "scripts\$name") -TargetUser $user | Out-Null }
        catch { $rejected = $_.Exception.Message -like '*elevated PowerShell*' }
        if (-not $rejected) { throw "$name accepted a non-administrator." }
    }
    $rejected = $false
    try { & (Join-Path $repo 'scripts\Install-TaskTrackerV3.ps1') -ConfigPath (Join-Path $repo 'missing.json') | Out-Null }
    catch { $rejected = $_.Exception.Message -like '*elevated PowerShell*' }
    if (-not $rejected) { throw 'Install-TaskTrackerV3.ps1 accepted a non-administrator.' }
}
Write-Output 'Administrative entrypoints parse and reject non-elevated execution'
