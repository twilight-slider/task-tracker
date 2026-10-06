param([Parameter(Mandatory)][string]$ConfigPath)

$ErrorActionPreference = 'Stop'
$protected = Split-Path -Parent $PSCommandPath
$support = Join-Path $protected 'TaskTracker-AdminCommon.ps1'
$expected = Join-Path $protected 'bootstrap.json'
$bootstrap = [IO.File]::ReadAllText($expected, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
$trusted = @('S-1-5-18', 'S-1-5-32-544')
if ([string]$bootstrap.ownerSid -match '^S-1-5-21-(?:[0-9]+-){3}[0-9]+$' -and
    [string]$bootstrap.serviceName -ceq ('TaskTracker-' + ([string]$bootstrap.ownerSid).Split('-')[-1])) {
    $service = Get-CimInstance Win32_Service -Filter "Name='$($bootstrap.serviceName)'" -ErrorAction Stop
    if ($service) {
        $accountName = ([string]$service.StartName) -replace '^\.(?=\\)', [Environment]::MachineName
        $trusted += ([Security.Principal.NTAccount]$accountName).Translate([Security.Principal.SecurityIdentifier]).Value
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
if ([IO.Path]::GetFullPath($ConfigPath) -ine [IO.Path]::GetFullPath($expected)) {
    throw 'Rebuild must use the protected bootstrap JSON beside this script.'
}
$config = Read-TrackerUtf8File $expected | ConvertFrom-Json
Assert-TrackerProtectedFile -Path $expected -ServiceSid ([string]$config.serviceAccountSid) | Out-Null
if ($config.schemaVersion -ne 3 -or [IO.Path]::GetFullPath([string]$config.protectedRoot) -ine [IO.Path]::GetFullPath($protected)) {
    throw 'Protected bootstrap JSON is inconsistent.'
}
$repository = Assert-PlainDirectory ([string]$config.repositoryRoot)
Assert-TaskTrackerCheckout -RepositoryRoot $repository -ExpectedCommit ([string]$config.repositoryCommit) | Out-Null
$installer = Join-Path $repository 'scripts\Install-TaskTrackerV3.ps1'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw 'Versioned Install-TaskTrackerV3.ps1 is not yet present. Complete the installer stage before rebuilding.'
}
& $installer -ConfigPath $expected
