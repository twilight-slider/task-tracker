$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\TaskTracker-AdminCommon.ps1')

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$localName = $identity.Name.Split('\')[-1]
function Get-CimInstance {
    param([string]$ClassName, [string]$Filter)
    if ($ClassName -ne 'Win32_Service' -or $Filter -ne "Name='TaskTracker-$($identity.User.Value.Split('-')[-1])'") {
        throw 'Unexpected service lookup.'
    }
    [pscustomobject]@{ StartName = ".\$localName" }
}

$actual = Get-InstalledTrackerServiceSid -TargetSid $identity.User.Value
if ($actual -cne $identity.User.Value) { throw "Local service account SID mismatch: $actual" }
Write-Output 'Local service account SID resolution passed'
