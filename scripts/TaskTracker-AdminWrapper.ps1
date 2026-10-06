param([switch]$PrepareOnly)

$ErrorActionPreference = 'Stop'
$support = Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1'
$json = Join-Path $PSScriptRoot 'bootstrap.json'
$bootstrap = [IO.File]::ReadAllText($json, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
$trusted = @('S-1-5-18', 'S-1-5-32-544')
if ([string]$bootstrap.ownerSid -match '^S-1-5-21-(?:[0-9]+-){3}[0-9]+$' -and
    [string]$bootstrap.serviceName -ceq ('TaskTracker-' + ([string]$bootstrap.ownerSid).Split('-')[-1])) {
    $service = Get-CimInstance Win32_Service -Filter "Name='$($bootstrap.serviceName)'" -ErrorAction Stop
    if ($service) {
        $trusted += ([Security.Principal.NTAccount]$service.StartName).Translate([Security.Principal.SecurityIdentifier]).Value
    }
} else { throw 'Protected bootstrap identity is invalid.' }
$supportItem = Get-Item -LiteralPath $support -Force -ErrorAction Stop
if ($supportItem.PSIsContainer -or ($supportItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Protected support script must be plain.'
}
$supportAcl = Get-Acl -LiteralPath $support
if ($supportAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) {
    throw 'Protected support script has untrusted owner.'
}
$write = [Security.AccessControl.FileSystemRights]'Write, Modify, FullControl, Delete, ChangePermissions, TakeOwnership'
foreach ($rule in $supportAcl.Access) {
    if ($rule.AccessControlType -ne 'Allow') { continue }
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin $trusted -and ($rule.FileSystemRights -band $write)) {
        throw 'Protected support script permits untrusted write.'
    }
}
. $support
Assert-TrackerAdministrator
$item = Get-Item -LiteralPath $json -Force -ErrorAction Stop
if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Protected bootstrap JSON must be a plain file.' }
$config = Read-TrackerUtf8File $json | ConvertFrom-Json
if ($config.schemaVersion -ne 3 -or -not (Test-AbsoluteWindowsPath ([string]$config.repositoryRoot))) {
    throw 'Protected bootstrap JSON has an unsupported schema.'
}
if ([IO.Path]::GetFullPath([string]$config.protectedRoot) -ine [IO.Path]::GetFullPath($PSScriptRoot)) {
    throw 'Protected wrapper path differs from bootstrap JSON.'
}
foreach ($file in @($PSCommandPath, $support, $json)) {
    Assert-TrackerProtectedFile -Path $file -ServiceSid ([string]$config.serviceAccountSid) | Out-Null
}
$repository = Assert-PlainDirectory ([string]$config.repositoryRoot)
$commit = Assert-TaskTrackerCheckout -RepositoryRoot $repository -ExpectedCommit ([string]$config.repositoryCommit)
$settings = Get-TargetTrackerSettings -TargetUser ([string]$config.targetUser)
$installedServiceSid = Get-InstalledTrackerServiceSid -TargetSid $settings.TargetSid
Assert-TrackerRootBoundary -TrackerRoot $settings.TrackerRoot -TargetSid $settings.TargetSid `
    -ServiceSid $installedServiceSid -TrustAuthenticatedUsers ([bool]$config.trustAuthenticatedUsers) | Out-Null
if ($settings.TargetSid -cne [string]$config.ownerSid -or
    $settings.TrackerRoot -ine [string]$config.trackerRoot -or
    $settings.EnvPath -ine [string]$config.targetEnvPath) {
    throw 'TargetUser env/profile differs from bootstrap JSON. Rerun administrative bootstrap.'
}
if ($installedServiceSid -and $installedServiceSid -cne [string]$config.serviceAccountSid) { throw 'Installed service SID differs from bootstrap JSON.' }
Assert-TrackerProtectedArea -TrackerRoot $settings.TrackerRoot -TargetSid $settings.TargetSid -ServiceSid ([string]$config.serviceAccountSid) | Out-Null
$builderSource = Join-Path $repository 'scripts\Rebuild-TaskTracker.ps1'
$builderFile = Get-Item -LiteralPath $builderSource -Force -ErrorAction Stop
if ($builderFile.PSIsContainer -or ($builderFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Versioned rebuild script must be a plain file.'
}
$builder = Join-Path $PSScriptRoot 'Rebuild-TaskTracker.ps1'
if (Test-Path -LiteralPath $builder) {
    $existing = Get-Item -LiteralPath $builder -Force
    if ($existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Protected rebuild script must be a plain file.' }
}
$builderTemp = Join-Path $PSScriptRoot ('.rebuild-' + [guid]::NewGuid().ToString('N') + '.ps1')
try {
    Copy-Item -LiteralPath $builderSource -Destination $builderTemp
    Move-Item -LiteralPath $builderTemp -Destination $builder -Force
} finally { if (Test-Path -LiteralPath $builderTemp) { Remove-Item -LiteralPath $builderTemp } }
Assert-TrackerProtectedFile -Path $builder -ServiceSid ([string]$config.serviceAccountSid) | Out-Null
Write-Output "Protected rebuild script ready: $builder; repository commit: $commit"
if (-not $PrepareOnly) { & $builder -ConfigPath $json }
