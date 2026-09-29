param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $ValidateOnly -and -not $elevated) { throw 'Installation requires an elevated PowerShell window.' }
if (-not [IO.Path]::IsPathFullyQualified($ConfigPath)) { throw 'ConfigPath must be absolute.' }
$requestFile = Get-Item -LiteralPath $ConfigPath -Force -ErrorAction Stop
if ($requestFile.PSIsContainer -or ($requestFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Bootstrap request must be a regular file.' }
$request = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$root = [string]$request.trackerRoot
if (-not [IO.Path]::IsPathFullyQualified($root)) { throw 'Tracker root must be absolute.' }
$root = [IO.Path]::GetFullPath($root).TrimEnd('\')
$volume = [IO.Path]::GetPathRoot($root)
if ($root -eq $volume.TrimEnd('\')) { throw 'Tracker root cannot be a volume root.' }
$rid = $identity.User.Value.Split('-')[-1]
if ($request.schemaVersion -ne 2 -or $request.ownerSid -ne $identity.User.Value -or
    $request.agentSid -ne $identity.User.Value -or $request.trackerRoot -ne $root -or
    $request.tasksRoot -ne (Join-Path $root 'tasks') -or
    $request.protectedRoot -ne (Join-Path $root '.protected') -or
    $request.installRoot -ne (Join-Path $root '.protected\bin') -or
    $request.serviceName -ne "TaskTracker-$rid" -or $request.pipeName -ne "task-tracker-$rid-v1" -or
    $request.serviceAccountName -notmatch '^[\p{L}\p{N}._-]{1,20}$' -or
    $request.nodePath -ne 'C:\Program Files\nodejs\node.exe') {
    throw 'Bootstrap request is inconsistent with this user or Tracker layout.'
}
if (-not (Test-Path -LiteralPath $request.nodePath -PathType Leaf)) { throw 'Node executable is missing.' }
$account = Get-LocalUser -Name $request.serviceAccountName -ErrorAction SilentlyContinue
if ($request.serviceAccountSid -and (-not $account -or $request.serviceAccountSid -ne $account.SID.Value)) {
    throw 'Service account SID changed after bootstrap.'
}
if ($account) {
    if ($account.SID.Value -eq $identity.User.Value) { throw 'Service account cannot be the agent account.' }
    $adminGroup = Get-LocalGroup -SID 'S-1-5-32-544'
    if (Get-LocalGroupMember -Group $adminGroup.Name -ErrorAction SilentlyContinue | Where-Object { $_.SID.Value -eq $account.SID.Value }) {
        throw 'Existing service account is an administrator. Supply a dedicated standard account.'
    }
}
$service = Get-Service -Name $request.serviceName -ErrorAction SilentlyContinue
$installedConfig = Join-Path $request.protectedRoot 'service.json'
$exe = Join-Path $request.installRoot 'TaskTrackerService.exe'
if ($service) {
    $actual = Get-CimInstance Win32_Service -Filter "Name='$($request.serviceName)'"
    if ($actual.PathName -ne "`"$exe`" --config `"$installedConfig`"" -or
        $actual.StartName -notmatch ('(?i)(^|\\)' + [regex]::Escape($request.serviceAccountName) + '$')) {
        throw 'Existing service has a different executable or account. Move requires a full reinstall.'
    }
    if (-not (Test-Path -LiteralPath $installedConfig)) { throw 'Existing service config is missing.' }
    $pinned = Get-Content -LiteralPath $installedConfig -Raw | ConvertFrom-Json
    if ($pinned.trackerRoot -ne $root) { throw 'Existing service is pinned to another Tracker.' }
}

# The agent must not own or change ACL of any existing non-volume ancestor.
$agentSids = @($identity.User.Value, 'S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
$danger = [Security.AccessControl.FileSystemRights]'ChangePermissions, TakeOwnership, DeleteSubdirectoriesAndFiles'
function Assert-AgentCannotAlter([string]$path, [bool]$volumeRoot) {
    $acl = Get-Acl -LiteralPath $path
    foreach ($rule in $acl.Access) {
        $sid = try { $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { $null }
        if ($sid -in $agentSids -and $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::InheritOnly -and
            $rule.AccessControlType -eq 'Allow' -and
            (($rule.FileSystemRights -band $danger) -or (-not $volumeRoot -and
            ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Delete)))) {
            throw "Agent can alter Tracker path: $path ($sid)"
        }
    }
}
$cursor = [IO.Path]::GetDirectoryName($root)
while ($cursor) {
    if (Test-Path -LiteralPath $cursor) {
        $item = Get-Item -LiteralPath $cursor -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe parent: $cursor" }
        $acl = Get-Acl -LiteralPath $cursor
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($cursor -ne $volume -and $owner -eq $identity.User.Value) { throw "Agent owns Tracker parent: $cursor" }
        Assert-AgentCannotAlter $cursor ($cursor -eq $volume)
    }
    if ($cursor -eq $volume) { break }
    $cursor = [IO.Path]::GetDirectoryName($cursor)
}
if (Test-Path -LiteralPath $root) {
    $item = Get-Item -LiteralPath $root -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Tracker root is not a plain directory.' }
    $acl = Get-Acl -LiteralPath $root
    $rootOwner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($rootOwner -ne $identity.User.Value -and (-not $account -or $rootOwner -ne $account.SID.Value)) {
        throw 'Existing Tracker root belongs to an unexpected account.'
    }
    if ($rootOwner -eq $account.SID.Value -and -not $acl.AreAccessRulesProtected) {
        throw 'Existing service-owned Tracker root has inherited ACL.'
    }
}
if ($ValidateOnly) { Write-Output "VALID: $root; service: $($request.serviceName)"; return }
Write-Output "Installing Tracker: $root; service: $($request.serviceName); account: $($request.serviceAccountName)"

$password = $null
if (-not $account) {
    $password = Read-Host "Password for $($request.serviceAccountName)" -AsSecureString
    $account = New-LocalUser -Name $request.serviceAccountName -Password $password -Description 'Personal Task Tracker service'
} elseif (-not $service) {
    $password = Read-Host "Password for existing $($request.serviceAccountName)" -AsSecureString
}
Add-Type -Path (Join-Path $PSScriptRoot 'ServiceLogonRight.cs')
$sidBytes = [byte[]]::new($account.SID.BinaryLength)
$account.SID.GetBinaryForm($sidBytes, 0)
[TaskFolderServiceLogonRight]::Grant($sidBytes) | Out-Null
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
$serviceSid = $account.SID
$agentSid = $identity.User

function Set-DirectoryRights([string]$path, $owner, [bool]$agentRead) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain directory: $path" }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($owner)
    foreach ($entry in @(@($system, 'FullControl'), @($admins, 'FullControl'), @($serviceSid, 'FullControl'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($entry[0], $entry[1], 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
    }
    if ($agentRead) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agentSid, 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None', 'Allow')) }
    Set-Acl -LiteralPath $path -AclObject $acl
}
function Set-FileRights([string]$path, [bool]$agentRead) {
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a plain file: $path" }
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($serviceSid)
    foreach ($entry in @(@($system, 'FullControl'), @($admins, 'FullControl'), @($serviceSid, 'FullControl'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($entry[0], $entry[1], 'Allow'))
    }
    if ($agentRead) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agentSid, 'ReadAndExecute', 'Allow')) }
    Set-Acl -LiteralPath $path -AclObject $acl
}

$parent = [IO.Path]::GetDirectoryName($root)
$missing = @()
for ($path = $parent; -not (Test-Path -LiteralPath $path); $path = [IO.Path]::GetDirectoryName($path)) { $missing = @($path) + $missing }
foreach ($path in $missing) { Set-DirectoryRights $path $admins $true }
# Freeze the current ACL of each immediate child before replacing the root's inherited ACL.
if (Test-Path -LiteralPath $root) {
    foreach ($child in Get-ChildItem -LiteralPath $root -Force) {
        if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Tracker child is a reparse point: $($child.FullName)" }
        $childAcl = Get-Acl -LiteralPath $child.FullName
        if (-not $childAcl.AreAccessRulesProtected) {
            $childAcl.SetAccessRuleProtection($true, $true)
            Set-Acl -LiteralPath $child.FullName -AclObject $childAcl
        }
    }
}
Set-DirectoryRights $root $serviceSid $true
if (Test-Path -LiteralPath $request.tasksRoot) {
    $tasks = Get-Item -LiteralPath $request.tasksRoot -Force
    $tasksAcl = Get-Acl -LiteralPath $request.tasksRoot
    if (-not $tasks.PSIsContainer -or ($tasks.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        -not $tasksAcl.AreAccessRulesProtected) { throw 'Existing tasks directory must retain its protected transitional ACL.' }
} else { Set-DirectoryRights $request.tasksRoot $serviceSid $true }
Set-DirectoryRights $request.protectedRoot $serviceSid $false
foreach ($name in @('bin', 'state', 'logs')) { Set-DirectoryRights (Join-Path $request.protectedRoot $name) $serviceSid $false }

$repo = Split-Path -Parent $PSScriptRoot
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw 'The .NET Framework C# compiler was not found.' }
if ($service -and $service.Status -eq 'Running') { Stop-Service -Name $request.serviceName -ErrorAction Stop }
& $csc /nologo /target:exe "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs')
if ($LASTEXITCODE -ne 0) { throw 'Service host compilation failed.' }
foreach ($name in @('folder-worker.js', 'task-folder.js')) {
    Copy-Item -LiteralPath (Join-Path $repo "src\$name") -Destination $request.installRoot -Force
}
foreach ($name in @('TaskTrackerService.exe', 'folder-worker.js', 'task-folder.js')) { Set-FileRights (Join-Path $request.installRoot $name) $false }
Copy-Item -LiteralPath (Join-Path $repo 'src\mcp-adapter.js') -Destination $root -Force
Set-FileRights (Join-Path $root 'mcp-adapter.js') $true
$clientConfig = Join-Path $root 'tracker-client.json'
[IO.File]::WriteAllText($clientConfig, ((@{ pipeName = $request.pipeName } | ConvertTo-Json -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
Set-FileRights $clientConfig $true
$manifestPath = Join-Path $request.protectedRoot 'projects.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    [IO.File]::WriteAllText($manifestPath, '{"schema_version":1,"projects":[]}' + "`n", [Text.UTF8Encoding]::new($false))
}
Set-FileRights $manifestPath $false
$config = [ordered]@{
    schemaVersion = 1; trackerRoot = $root; serviceName = $request.serviceName
    serviceAccountSid = $serviceSid.Value; agentSid = $agentSid.Value; pipeName = $request.pipeName
    nodePath = $request.nodePath; tasksRoot = $request.tasksRoot
    protectedRoot = $request.protectedRoot; installRoot = $request.installRoot
}
[IO.File]::WriteAllText($installedConfig, (($config | ConvertTo-Json -Depth 3) + "`n"), [Text.UTF8Encoding]::new($false))
Set-FileRights $installedConfig $false
if (-not $service) {
    $credential = [pscredential]::new(".\$($request.serviceAccountName)", $password)
    New-Service -Name $request.serviceName -BinaryPathName "`"$exe`" --config `"$installedConfig`"" `
        -Credential $credential -StartupType Automatic -Description 'Personal Task Tracker service' | Out-Null
}
Start-Service -Name $request.serviceName -ErrorAction Stop
Write-Output "Installed: $($request.serviceName); Tracker: $root"
