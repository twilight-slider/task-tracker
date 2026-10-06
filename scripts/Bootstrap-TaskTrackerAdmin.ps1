param(
    [Parameter(Mandatory)][string]$TargetUser,
    [string]$ServiceAccountName,
    [string[]]$SnapshotRoots,
    [switch]$TrustAuthenticatedUsers
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1')
. (Join-Path $PSScriptRoot 'TaskTracker-InstallLayout.ps1')
Assert-TrackerAdministrator
$repository = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$commit = Assert-TaskTrackerCheckout -RepositoryRoot $repository
$settings = Get-TargetTrackerSettings -TargetUser $TargetUser
$tracker = Assert-PlainDirectory $settings.TrackerRoot
$installedServiceSid = Get-InstalledTrackerServiceSid -TargetSid $settings.TargetSid
Assert-TrackerRootBoundary -TrackerRoot $tracker -TargetSid $settings.TargetSid -ServiceSid $installedServiceSid `
    -TrustAuthenticatedUsers ([bool]$TrustAuthenticatedUsers) | Out-Null
$protected = Join-Path $tracker '.protected'
if (-not (Test-Path -LiteralPath $protected -PathType Container)) {
    throw "Protected Tracker area is absent: $protected. Run Prepare-TrackerRoot.ps1 -TargetUser '$TargetUser' as administrator, then rerun bootstrap."
}
$serviceSid = $null
$installed = Join-Path $protected 'service.json'
if (Test-Path -LiteralPath $installed -PathType Leaf) {
    try { $serviceSid = [string]((Read-TrackerUtf8File $installed | ConvertFrom-Json).serviceAccountSid) } catch { }
}
if ($serviceSid -and $installedServiceSid -and $serviceSid -cne $installedServiceSid) { throw 'Installed service SID differs from service.json.' }
Assert-TrackerProtectedArea -TrackerRoot $tracker -TargetSid $settings.TargetSid -ServiceSid $installedServiceSid | Out-Null
$owner = (Get-Acl -LiteralPath $tracker).GetOwner([Security.Principal.SecurityIdentifier]).Value
if ($owner -eq $settings.TargetSid) {
    throw "Tracker is owned by TargetUser and cannot be protected against that owner: $tracker. Choose an administrator/service-owned root; bootstrap will not change owner."
}
if (-not $ServiceAccountName) {
    $existingService = Get-CimInstance Win32_Service -Filter "Name='TaskTracker-$($settings.TargetSid.Split('-')[-1])'" -ErrorAction Stop
    if ($existingService) {
        $ServiceAccountName = ([string]$existingService.StartName).Split('\')[-1]
    } else {
        $shortName = $TargetUser.Split('\')[-1]
        $ServiceAccountName = "$shortName-TT"
    }
}
if ($ServiceAccountName -notmatch '^[\p{L}\p{N}._-]{1,20}$') {
    throw 'ServiceAccountName must be a Windows account name of at most 20 characters.'
}
$account = Get-LocalUser -Name $ServiceAccountName -ErrorAction SilentlyContinue
$installedConfig = Join-Path $protected 'service.json'
if ($PSBoundParameters.ContainsKey('SnapshotRoots')) {
    $snapshotRootsToInstall = @($SnapshotRoots)
} elseif (Test-Path -LiteralPath $installedConfig -PathType Leaf) {
    $snapshotRootsToInstall = @((Read-TrackerUtf8File $installedConfig | ConvertFrom-Json).snapshotRoots | Where-Object { $_ })
} else {
    throw 'First installation requires -SnapshotRoots <absolute project root>. Rerun bootstrap with at least one allowed root.'
}
if ($snapshotRootsToInstall.Count -eq 0) { throw 'SnapshotRoots cannot be empty.' }
$snapshotRootsToInstall = @(Assert-TrackerSnapshotRoots -Roots $snapshotRootsToInstall -TrackerRoot $tracker)
$rid = $settings.TargetSid.Split('-')[-1]
$manifest = Join-Path $repository 'vendor\release-manifest.json'
$manifestFile = Get-Item -LiteralPath $manifest -Force -ErrorAction Stop
if ($manifestFile.PSIsContainer -or ($manifestFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Release manifest must be a plain file: $manifest"
}
$manifestHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
$config = [ordered]@{
    schemaVersion = 3
    targetUser = $TargetUser
    ownerSid = $settings.TargetSid
    agentSid = $settings.TargetSid
    targetEnvPath = $settings.EnvPath
    trackerRoot = $tracker
    tasksRoot = (Join-Path $tracker 'tasks')
    protectedRoot = $protected
    installRoot = (Join-Path $protected 'bin')
    runtimeRoot = (Join-Path $protected 'runtime')
    serviceName = "TaskTracker-$rid"
    serviceAccountName = $ServiceAccountName
    serviceAccountSid = if ($installedServiceSid) { $installedServiceSid } elseif ($account) { $account.SID.Value } else { $serviceSid }
    pipeName = "task-tracker-$rid-v1"
    mcpPort = 38772
    repositoryRoot = $repository
    repositoryCommit = $commit
    releaseManifestSha256 = $manifestHash
    trustAuthenticatedUsers = [bool]$TrustAuthenticatedUsers
    snapshotRoots = $snapshotRootsToInstall
    marketplaceSource = 'https://github.com/twilight-slider/ai-marketplace.git'
}
$json = Join-Path $protected 'bootstrap.json'
$wrapper = Join-Path $protected 'TaskTracker-AdminWrapper.ps1'
$common = Join-Path $protected 'TaskTracker-AdminCommon.ps1'
foreach ($target in @($json, $wrapper, $common)) {
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Protected bootstrap target is not a plain file: $target" }
    }
}
$temporary = Join-Path $protected ('.bootstrap-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    [IO.File]::WriteAllText($temporary, (($config | ConvertTo-Json -Depth 5) + "`n"), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $json -Force
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
foreach ($entry in @(
    @{ Source = (Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1'); Destination = $common },
    @{ Source = (Join-Path $PSScriptRoot 'TaskTracker-AdminWrapper.ps1'); Destination = $wrapper }
)) {
    $temp = Join-Path $protected ('.bootstrap-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Copy-Item -LiteralPath $entry.Source -Destination $temp
        Move-Item -LiteralPath $temp -Destination $entry.Destination -Force
    } finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp } }
}
foreach ($file in @($json, $common, $wrapper)) {
    Assert-TrackerProtectedFile -Path $file -ServiceSid $serviceSid | Out-Null
}
Assert-TrackerProtectedArea -TrackerRoot $tracker -TargetSid $settings.TargetSid -ServiceSid $serviceSid | Out-Null
Write-Output "Administrative bootstrap ready: $json; wrapper: $wrapper; Tracker owner preserved: $owner"
