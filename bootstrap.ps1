param(
    [Parameter(Mandatory)][string]$TargetUser,
    [string]$ServiceAccountName,
    [int]$McpPort,
    [string[]]$SnapshotRoots,
    [switch]$TrustAuthenticatedUsers
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\TaskTracker-AdminCommon.ps1')
Assert-TrackerAdministrator
Assert-TaskTrackerCheckout -RepositoryRoot $PSScriptRoot | Out-Null
if ($PSBoundParameters.ContainsKey('SnapshotRoots') -and @($SnapshotRoots).Count -eq 0) {
    throw 'SnapshotRoots cannot be empty.'
}
if (-not $PSBoundParameters.ContainsKey('SnapshotRoots')) {
    $settings = Get-TargetTrackerSettings -TargetUser $TargetUser
    $PSBoundParameters['SnapshotRoots'] = @((Read-GitFolderFromEnv -EnvPath $settings.EnvPath))
}
& (Join-Path $PSScriptRoot 'scripts\Prepare-TrackerRoot.ps1') -TargetUser $TargetUser `
    -TrustAuthenticatedUsers:$TrustAuthenticatedUsers
& (Join-Path $PSScriptRoot 'scripts\Bootstrap-TaskTrackerAdmin.ps1') @PSBoundParameters
$settings = Get-TargetTrackerSettings -TargetUser $TargetUser
& (Join-Path $settings.TrackerRoot '.protected\TaskTracker-AdminWrapper.ps1')
