param(
    [Parameter(Mandatory)][string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run installer in an elevated PowerShell window.'
}
$requested = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$isTest = $requested.tasksRoot -eq 'D:\Projects\Tracker\task-folder-mcp-tests\tasks' -and
    $requested.protectedRoot -eq 'C:\ProgramData\TaskFolderMcp\protected-test'
$isProduction = $requested.tasksRoot -eq 'D:\Projects\Tracker\tasks' -and
    $requested.protectedRoot -eq 'C:\ProgramData\TaskFolderMcp\protected'
if ($requested.schemaVersion -ne 1 -or $requested.ownerSid -ne $identity.User.Value -or
    $requested.agentSid -ne $identity.User.Value -or $requested.serviceName -ne 'TaskFolderMcp' -or
    $requested.serviceAccountName -ne 'TaskFolderMcpSvc' -or $requested.pipeName -ne 'task-folder-mcp-v1' -or
    (-not $isTest -and -not $isProduction) -or
    $requested.installRoot -ne 'C:\Program Files\TaskFolderMcp' -or
    $requested.nodePath -ne 'C:\Program Files\nodejs\node.exe') {
    throw 'Bootstrap configuration differs from the reviewed test or production layout.'
}
$account = Get-LocalUser -Name $requested.serviceAccountName -ErrorAction SilentlyContinue
if ($account -and $requested.serviceAccountSid -and $requested.serviceAccountSid -ne $account.SID.Value) {
    throw 'Service account SID changed after bootstrap.'
}
$password = $null
if (-not $account) {
    $password = Read-Host 'Password for new TaskFolderMcpSvc account' -AsSecureString
    $account = New-LocalUser -Name $requested.serviceAccountName -Password $password -Description 'Task Folder MCP Windows service'
} elseif (-not (Get-Service -Name $requested.serviceName -ErrorAction SilentlyContinue)) {
    $password = Read-Host 'Password for existing TaskFolderMcpSvc account' -AsSecureString
}
$rightSource = Join-Path $PSScriptRoot 'ServiceLogonRight.cs'
Add-Type -Path $rightSource
$sidBytes = [byte[]]::new($account.SID.BinaryLength)
$account.SID.GetBinaryForm($sidBytes, 0)
if ([TaskFolderServiceLogonRight]::Grant($sidBytes)) {
    Write-Output 'Granted SeServiceLogonRight to TaskFolderMcpSvc'
}

function Set-DirectoryAcl([string]$Path, [hashtable]$Rights) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing non-directory or reparse point: $Path"
        }
    } else { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach ($sid in $Rights.Keys) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), $Rights[$sid],
            [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-FileAcl([string]$Path, [hashtable]$Rights) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing non-file or reparse point: $Path"
    }
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach ($sid in $Rights.Keys) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), $Rights[$sid],
            [Security.AccessControl.AccessControlType]::Allow))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$system = 'S-1-5-18'
$admins = 'S-1-5-32-544'
$serviceSid = $account.SID.Value
$agentSid = $requested.agentSid
$full = [Security.AccessControl.FileSystemRights]::FullControl
$read = [Security.AccessControl.FileSystemRights]'ReadAndExecute'
$base = 'C:\ProgramData\TaskFolderMcp'
if ($isTest) {
    $tasksParent = Split-Path -Parent $requested.tasksRoot
    Set-DirectoryAcl $tasksParent (@{ $system = $full; $admins = $full; $agentSid = $full })
    Set-DirectoryAcl $requested.tasksRoot (@{ $system = $full; $admins = $full; $agentSid = $full })
} else {
    $tasks = Get-Item -LiteralPath $requested.tasksRoot -Force -ErrorAction Stop
    if (-not $tasks.PSIsContainer -or ($tasks.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Production task root must be an existing regular directory: $($requested.tasksRoot)"
    }
}
Set-DirectoryAcl $base (@{ $system = $full; $admins = $full; $serviceSid = $full })
Set-DirectoryAcl $requested.protectedRoot (@{ $system = $full; $admins = $full; $serviceSid = $full })
Set-DirectoryAcl $requested.installRoot (@{ $system = $full; $admins = $full; $serviceSid = $read; $agentSid = $read })
$manifestPath = Join-Path $requested.protectedRoot 'projects.json'
if ($isProduction) {
    & (Join-Path $PSScriptRoot 'Initialize-ProductionRegistry.ps1') `
        -SourcePath (Join-Path $requested.tasksRoot 'projects.json') -DestinationPath $manifestPath | Out-Null
} elseif (-not (Test-Path -LiteralPath $manifestPath)) {
    $manifest = @{ schema_version = 1; projects = @(@{
        project_key = 'TFMTEST'; source_type = 'NO_JIRA'; next_issue_number = 1
    }) }
    [IO.File]::WriteAllText($manifestPath, (($manifest | ConvertTo-Json -Depth 4) + "`n"), [Text.UTF8Encoding]::new($false))
}
Set-FileAcl $manifestPath (@{ $system = $full; $admins = $full; $serviceSid = $full })

$service = Get-Service -Name $requested.serviceName -ErrorAction SilentlyContinue
$wasRunning = $service -and $service.Status -eq 'Running'
if ($service) {
    $actual = Get-CimInstance Win32_Service -Filter "Name='$($requested.serviceName)'"
    $expectedExe = Join-Path $requested.installRoot 'TaskFolderMcpService.exe'
    if ($actual.PathName -ne "`"$expectedExe`" --config `"$base\service.json`"" -or
        $actual.StartName -notmatch '(?i)(^|\\)TaskFolderMcpSvc$') {
        throw 'An existing service has a different executable or account.'
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw 'The .NET Framework C# compiler was not found.' }
$exe = Join-Path $requested.installRoot 'TaskFolderMcpService.exe'
$sourceNames = @('ServiceHost.cs', 'folder-worker.js', 'task-folder.js', 'mcp-adapter.js')
$installedNames = @('TaskFolderMcpService.exe', 'folder-worker.js', 'task-folder.js', 'mcp-adapter.js')
$sourceHashes = [ordered]@{}
for ($i = 0; $i -lt $sourceNames.Count; $i++) {
    $sourceHashes[$sourceNames[$i]] = (Get-FileHash -LiteralPath (Join-Path $repo "src\$($sourceNames[$i])") -Algorithm SHA256).Hash
}
$statePath = Join-Path $base 'install-state.json'
$previous = if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } else { $null }
$codeChanged = -not $service -or -not $previous -or $previous.schemaVersion -ne 1
for ($i = 0; $i -lt $sourceNames.Count; $i++) {
    $installedFile = Join-Path $requested.installRoot $installedNames[$i]
    if (-not (Test-Path -LiteralPath $installedFile) -or
        $previous.sourceHashes.($sourceNames[$i]) -ne $sourceHashes[$sourceNames[$i]] -or
        $previous.installedHashes.($installedNames[$i]) -ne (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256 -ErrorAction SilentlyContinue).Hash) {
        $codeChanged = $true
    }
}
if ($codeChanged) {
    if ($service -and $service.Status -eq 'Running') { Stop-Service -Name $requested.serviceName -ErrorAction Stop }
    & $csc /nologo /target:exe "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Service host compilation failed.' }
    foreach ($name in @('folder-worker.js', 'task-folder.js', 'mcp-adapter.js')) {
        Copy-Item -LiteralPath (Join-Path $repo "src\$name") -Destination $requested.installRoot -Force
    }
}
foreach ($file in @($exe, (Join-Path $requested.installRoot 'folder-worker.js'),
        (Join-Path $requested.installRoot 'task-folder.js'), (Join-Path $requested.installRoot 'mcp-adapter.js'))) {
    Set-FileAcl $file (@{ $system = $full; $admins = $full; $serviceSid = $read; $agentSid = $read })
}

$installedConfig = Join-Path $base 'service.json'
$config = [ordered]@{
    schemaVersion = 1
    serviceName = $requested.serviceName
    serviceAccountSid = $serviceSid
    agentSid = $agentSid
    pipeName = $requested.pipeName
    nodePath = $requested.nodePath
    tasksRoot = $requested.tasksRoot
    protectedRoot = $requested.protectedRoot
    installRoot = $requested.installRoot
}
$configText = ($config | ConvertTo-Json -Depth 3) + "`n"
$configChanged = -not (Test-Path -LiteralPath $installedConfig) -or
    [IO.File]::ReadAllText($installedConfig) -ne $configText
if ($configChanged) {
    if ($service -and $service.Status -eq 'Running' -and -not $codeChanged) {
        Stop-Service -Name $requested.serviceName -ErrorAction Stop
    }
    [IO.File]::WriteAllText($installedConfig, $configText, [Text.UTF8Encoding]::new($false))
}
Set-FileAcl $installedConfig (@{ $system = $full; $admins = $full; $serviceSid = $read })
if (-not $service) {
    $credential = [pscredential]::new(".\$($requested.serviceAccountName)", $password)
    New-Service -Name $requested.serviceName -BinaryPathName "`"$exe`" --config `"$installedConfig`"" -Credential $credential -StartupType Automatic -Description 'Task Folder MCP protected folder service' | Out-Null
}
$adminGroup = Get-LocalGroup -SID $admins
$wasAdmin = [bool](Get-LocalGroupMember -Group $adminGroup.Name -ErrorAction SilentlyContinue | Where-Object { $_.SID.Value -eq $serviceSid })
if ($wasAdmin) {
    Remove-LocalGroupMember -Group $adminGroup.Name -Member $account.Name
}
if ($wasAdmin -and (Get-Service -Name $requested.serviceName).Status -eq 'Running') {
    Stop-Service -Name $requested.serviceName -ErrorAction Stop
}
$installedHashes = [ordered]@{}
foreach ($name in $installedNames) {
    $installedHashes[$name] = (Get-FileHash -LiteralPath (Join-Path $requested.installRoot $name) -Algorithm SHA256).Hash
}
$state = [ordered]@{ schemaVersion = 1; sourceHashes = $sourceHashes; installedHashes = $installedHashes }
$stateText = ($state | ConvertTo-Json -Depth 4) + "`n"
if (-not (Test-Path -LiteralPath $statePath) -or [IO.File]::ReadAllText($statePath) -ne $stateText) {
    [IO.File]::WriteAllText($statePath, $stateText, [Text.UTF8Encoding]::new($false))
}
Set-FileAcl $statePath (@{ $system = $full; $admins = $full; $serviceSid = $read })
if ((Get-Service -Name $requested.serviceName).Status -ne 'Running') {
    try { Start-Service -Name $requested.serviceName -ErrorAction Stop }
    catch {
        $event = Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Service Control Manager'; StartTime = (Get-Date).AddMinutes(-2) } -ErrorAction SilentlyContinue |
            Where-Object { $_.Message -match 'TaskFolderMcp' } | Select-Object -First 1
        if ($event) { throw "TaskFolderMcp did not start: $($event.Message)" }
        throw
    }
}
if ($codeChanged -or $configChanged -or $wasAdmin -or -not $service -or -not $wasRunning) {
    Write-Output "Installed/updated $($requested.serviceName); tasks: $($requested.tasksRoot); protected: $($requested.protectedRoot)"
} else {
    Write-Output "Already installed and running: $($requested.serviceName)"
}
