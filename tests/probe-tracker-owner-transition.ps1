param([Parameter(Mandatory)][string]$ServiceAccountSid)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Owner transition probe requires elevated PowerShell.'
}
$service = [Security.Principal.SecurityIdentifier]$ServiceAccountSid
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $repo ('.runtime\tests\A60-04-owner-transition\run-' + $PID)
try {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $file = Join-Path $root 'probe.txt'
    [IO.File]::WriteAllText($file, 'probe')
    foreach ($entry in @(@($file, $false), @($root, $true))) {
        $path = [string]$entry[0]
        $directory = [bool]$entry[1]
        $acl = if ($directory) { [Security.AccessControl.DirectorySecurity]::new() }
            else { [Security.AccessControl.FileSecurity]::new() }
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner($service)
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $service.Value)) {
            $rule = if ($directory) {
                [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
            } else { [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow') }
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $path -AclObject $acl
        $actual = Get-Acl -LiteralPath $path
        if ($actual.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $service.Value) {
            throw "Owner transition failed: $path"
        }
        if ($directory) { [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($path), $actual) }
        else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($path), $actual) }
    }
    Write-Output 'OWNER PROBE PASSED: service-owned file and directory; repeated ACL write'
} finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
