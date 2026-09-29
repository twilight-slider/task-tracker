# Run once from an elevated 64-bit PowerShell session.
$ErrorActionPreference = 'Stop'
$name = 'TaskFolderMcpSvc'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell session.'
}
if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) {
    throw "Local account $name already exists; no changes were made."
}

$password = Read-Host "Password for $name" -AsSecureString
try {
    $account = New-LocalUser -Name $name -Password $password -Description 'Task Folder MCP service account' -ErrorAction Stop
    $administrators = Get-LocalGroup -SID 'S-1-5-32-544' -ErrorAction Stop
    Add-LocalGroupMember -Group $administrators -Member $account -ErrorAction Stop
    Write-Output "Created $env:COMPUTERNAME\$name (SID $($account.SID)); added to $($administrators.Name)."
} finally {
    $password.Dispose()
}
