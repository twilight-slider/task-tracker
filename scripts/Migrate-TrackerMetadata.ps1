param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [string]$LegacyConfigPath = 'C:\ProgramData\TaskFolderMcp\service.json',
    [switch]$Apply,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$root = [IO.Path]::GetFullPath([string]$config.trackerRoot).TrimEnd('\')
$tasks = Join-Path $root 'tasks'
if ($config.schemaVersion -ne 1 -or $config.tasksRoot -ne $tasks -or
    $config.protectedRoot -ne (Join-Path $root '.protected') -or
    -not $config.serviceAccountSid -or -not $config.agentSid) { throw 'Invalid installed Tracker configuration.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (($Apply -or $Rollback) -and -not $admin) { throw 'Metadata migration requires elevated PowerShell.' }
if ($Rollback) {
    if ((Get-Service -Name $config.serviceName -ErrorAction SilentlyContinue).Status -eq 'Running') {
        throw 'Stop the new Tracker service before rollback.'
    }
    $backupRoot = Join-Path $config.protectedRoot 'state\metadata-backups'
    $agentsBackup = Join-Path $root 'AGENTS.before-AIDEV-60.md'
    $projectsBackup = Join-Path $backupRoot 'tasks-projects-before.json'
    if (-not (Test-Path -LiteralPath $agentsBackup) -or -not (Test-Path -LiteralPath $projectsBackup)) {
        throw 'Metadata rollback backups are missing.'
    }
    $migrationMarker = Join-Path $config.protectedRoot 'state\aidev60-metadata-migrated.json'
    $currentRegistry = Join-Path $root 'projects.json'
    if (Test-Path -LiteralPath $migrationMarker) {
        $state = Get-Content -LiteralPath $migrationMarker -Raw | ConvertFrom-Json
        if (-not (Test-Path -LiteralPath $currentRegistry) -or
            (Get-FileHash -LiteralPath $currentRegistry -Algorithm SHA256).Hash -ne $state.registrySha256) {
            throw 'Tracker registry changed after migration; automatic rollback is unsafe.'
        }
    }
    Copy-Item -LiteralPath $agentsBackup -Destination (Join-Path $tasks 'AGENTS.md') -Force
    Copy-Item -LiteralPath $projectsBackup -Destination (Join-Path $tasks 'projects.json') -Force
    foreach ($path in @((Join-Path $root 'AGENTS.md'), (Join-Path $root 'projects.json'),
            (Join-Path $config.protectedRoot 'state\aidev60-metadata-migrated.json'))) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    $resolvedBackup = (Resolve-Path -LiteralPath $backupRoot).ProviderPath
    $expectedBackup = [IO.Path]::GetFullPath((Join-Path $root '.protected\state\metadata-backups'))
    if (-not $resolvedBackup.Equals($expectedBackup, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Metadata backup path is outside this Tracker.'
    }
    Remove-Item -LiteralPath $resolvedBackup -Recurse -Force
    Write-Output 'ROLLED BACK: original tasks metadata restored'
    return
}
$legacy = Get-Content -LiteralPath $legacyConfigPath -Raw | ConvertFrom-Json
if ($legacy.tasksRoot -ne $tasks) { throw 'Legacy service points to a different task root.' }
$sourceRegistry = Join-Path $legacy.protectedRoot 'projects.json'
$sourceItem = Get-Item -LiteralPath $sourceRegistry -Force
if ($sourceItem.PSIsContainer -or ($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Legacy project registry is not a regular file.'
}
$manifest = Get-Content -LiteralPath $sourceRegistry -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or @($manifest.projects).Count -lt 1) { throw 'Invalid legacy project registry.' }
$keys = @($manifest.projects | ForEach-Object project_key)
if (@($keys | Select-Object -Unique).Count -ne $keys.Count) { throw 'Duplicate legacy project key.' }
$template = Join-Path (Split-Path -Parent $PSScriptRoot) 'templates\AGENTS.md'
$oldAgents = Join-Path $tasks 'AGENTS.md'
$newAgents = Join-Path $root 'AGENTS.md'
$backupAgents = Join-Path $root 'AGENTS.before-AIDEV-60.md'
$oldRegistry = Join-Path $tasks 'projects.json'
$newRegistry = Join-Path $root 'projects.json'
$obsoleteRegistry = Join-Path $config.protectedRoot 'projects.json'
$marker = Join-Path $config.protectedRoot 'state\aidev60-metadata-migrated.json'
if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw 'Tracker AGENTS.md template is missing.' }
if (-not (Test-Path -LiteralPath $marker)) {
    if (-not (Test-Path -LiteralPath $oldAgents -PathType Leaf)) { throw 'Original tasks/AGENTS.md is missing.' }
    if (-not (Test-Path -LiteralPath $oldRegistry -PathType Leaf)) { throw 'Original tasks/projects.json is missing.' }
} elseif (-not (Test-Path -LiteralPath $newAgents -PathType Leaf) -or
    -not (Test-Path -LiteralPath $backupAgents -PathType Leaf) -or
    -not (Test-Path -LiteralPath $newRegistry -PathType Leaf)) {
    throw 'Metadata migration marker exists but files are incomplete.'
}
if (-not (Test-Path -LiteralPath $marker) -and (Test-Path -LiteralPath $newRegistry)) {
    $destination = Get-Content -LiteralPath $newRegistry -Raw | ConvertFrom-Json
    if (@($destination.projects).Count -gt 0) { throw 'New Tracker registry already contains projects; reconcile it before migration.' }
}
if (Test-Path -LiteralPath $obsoleteRegistry) {
    $obsolete = Get-Content -LiteralPath $obsoleteRegistry -Raw | ConvertFrom-Json
    if (@($obsolete.projects).Count -gt 0) { throw 'Old new-service registry contains projects; reconcile it before migration.' }
}
$backupRoot = Join-Path $config.protectedRoot 'state\metadata-backups'
if (-not (Test-Path -LiteralPath $marker) -and (Test-Path -LiteralPath $backupRoot)) {
    throw 'An incomplete metadata migration backup exists; recover it before retrying.'
}
if ((Test-Path -LiteralPath $marker) -and
    ((Test-Path -LiteralPath $oldAgents) -or (Test-Path -LiteralPath $oldRegistry) -or
     (Test-Path -LiteralPath $obsoleteRegistry))) {
    throw 'Metadata reappeared after migration; do not delete it automatically.'
}
Write-Output "READY: Tracker metadata from $tasks to $root"
if (-not $Apply) { return }
if ((Get-Service -Name TaskFolderMcp -ErrorAction SilentlyContinue).Status -eq 'Running') {
    throw 'Stop the legacy TaskFolderMcp service before metadata migration.'
}
if ((Get-Service -Name $config.serviceName -ErrorAction SilentlyContinue).Status -eq 'Running') {
    throw 'Stop the new Tracker service before metadata migration.'
}
if (Test-Path -LiteralPath $marker) {
    Write-Output 'Metadata already migrated'
    return
}
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $sourceRegistry -Destination (Join-Path $backupRoot 'legacy-projects-before.json')
Copy-Item -LiteralPath $oldRegistry -Destination (Join-Path $backupRoot 'tasks-projects-before.json')
Copy-Item -LiteralPath $oldAgents -Destination $backupAgents -Force
Copy-Item -LiteralPath $template -Destination $newAgents -Force

# Preserve the old service's counter, advancing it past any existing task folder.
foreach ($project in $manifest.projects) {
    if ($project.source_type -ne 'NO_JIRA') { continue }
    $counter = 0
    if (-not [int]::TryParse([string]$project.next_issue_number, [ref]$counter) -or $counter -lt 1) {
        throw "Invalid NO_JIRA counter: $($project.project_key)"
    }
    $maximum = 0
    foreach ($year in Get-ChildItem -LiteralPath $tasks -Directory -Force | Where-Object Name -Match '^\d{4}$') {
        foreach ($folder in Get-ChildItem -LiteralPath $year.FullName -Directory -Force) {
            if ($folder.Name -match ('^' + [regex]::Escape($project.project_key) + '-([1-9][0-9]*)$')) {
                $maximum = [Math]::Max($maximum, [int]$Matches[1])
            }
        }
    }
    $project.next_issue_number = [Math]::Max([int]$project.next_issue_number, $maximum + 1)
}
$temporary = Join-Path $config.protectedRoot ('projects-migration-' + $PID + '.tmp')
try {
    [IO.File]::WriteAllText($temporary, (($manifest | ConvertTo-Json -Depth 6) + "`n"), [Text.UTF8Encoding]::new($false))
    $validator = 'const fs=require("node:fs");const folder=require(process.argv[1]);folder.validateManifest(JSON.parse(fs.readFileSync(process.argv[2],"utf8")));'
    & $config.nodePath -e $validator (Join-Path (Split-Path -Parent $PSScriptRoot) 'src\task-folder.js') $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Migrated project registry failed validation.' }
    if (Test-Path -LiteralPath $newRegistry) {
        $destination = Get-Content -LiteralPath $newRegistry -Raw | ConvertFrom-Json
        if (@($destination.projects).Count -gt 0) { throw 'New Tracker registry changed during migration.' }
    }
    Move-Item -LiteralPath $temporary -Destination $newRegistry -Force
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }

$serviceSid = [Security.Principal.SecurityIdentifier]$config.serviceAccountSid
$agentSid = [Security.Principal.SecurityIdentifier]$config.agentSid
$system = [Security.Principal.SecurityIdentifier]'S-1-5-18'
$admins = [Security.Principal.SecurityIdentifier]'S-1-5-32-544'
foreach ($entry in @(@($newAgents, $true), @($backupAgents, $true), @($newRegistry, $false))) {
    $acl = Get-Acl -LiteralPath $entry[0]
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    $ownerChanged = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $serviceSid.Value
    if ($ownerChanged) {
        $acl.SetOwner($serviceSid)
    }
    foreach ($sid in @($system, $admins, $serviceSid)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow'))
    }
    if ($entry[1]) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($agentSid, 'ReadAndExecute', 'Allow')) }
    if ($ownerChanged) {
        $fresh = [Security.AccessControl.FileSecurity]::new()
        $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
        $fresh.SetSecurityDescriptorSddlForm($acl.GetSecurityDescriptorSddlForm($sections), $sections)
        Set-Acl -LiteralPath $entry[0] -AclObject $fresh
    } else { [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($entry[0]), $acl) }
}
try {
    [IO.File]::WriteAllText($marker, ((@{ migratedAt = (Get-Date).ToString('o'); legacyRegistry = $sourceRegistry;
        registrySha256 = (Get-FileHash -LiteralPath $newRegistry -Algorithm SHA256).Hash } |
        ConvertTo-Json -Compress) + "`n"), [Text.UTF8Encoding]::new($false))
    Remove-Item -LiteralPath $oldAgents -Force
    Remove-Item -LiteralPath $oldRegistry -Force
    if (Test-Path -LiteralPath $obsoleteRegistry) { Remove-Item -LiteralPath $obsoleteRegistry -Force }
} catch {
    $failure = $_
    & $PSCommandPath -ConfigPath $ConfigPath -LegacyConfigPath $LegacyConfigPath -Rollback
    throw $failure
}
Write-Output "MIGRATED: AGENTS.md, projects.json; backup $backupRoot"
