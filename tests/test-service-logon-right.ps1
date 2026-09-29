$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this policy inspection in an elevated PowerShell window.'
}
Add-Type -Path (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\ServiceLogonRight.cs')
$account = Get-LocalUser -Name 'TaskFolderMcpSvc' -ErrorAction Stop
$sid = [byte[]]::new($account.SID.BinaryLength)
$account.SID.GetBinaryForm($sid, 0)
if (-not [TaskFolderServiceLogonRight]::Has($sid)) {
    throw 'TaskFolderMcpSvc is missing SeServiceLogonRight.'
}
Write-Output 'Service logon right is present'
