param([Parameter(Mandatory)][string]$WorkspaceAgentUser)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'src\WorkspaceAgents.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$serviceSid = $identity.User.Value
$clientSid = 'S-1-5-21-1-2-3-1002'
$workspace = @(Resolve-TrackerWorkspaceAgents @($WorkspaceAgentUser, $WorkspaceAgentUser))
if ($workspace.Count -ne 1) { throw 'Workspace account was not deduplicated.' }
$config = [pscustomobject]@{ agentSid = $clientSid; serviceAccountSid = $serviceSid; workspaceAgents = @($workspace) }
if (@(Get-TrackerWorkspaceAgents $config).Count -ne 1) { throw 'Named account binding was not retained.' }
if (@(Get-TrackerWorkspaceAgents ([pscustomobject]@{})).Count) { throw 'Old config is not compatible.' }
$bootstrap = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repo 'scripts\Bootstrap-TaskTrackerAdmin.ps1'), [ref]$null, [ref]$null)
$selection = @($bootstrap.FindAll({ param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
    $node.Left.VariablePath.UserPath -eq 'workspaceAgents'
}, $true))[0]
if (-not $selection) { throw 'Workspace account selection is absent from bootstrap.' }
& {
    $existingWorkspaceAgents = @($workspace)
    Invoke-Expression $selection.Extent.Text
    if ($workspaceAgents.Count -ne 1 -or $workspaceAgents[0].sid -ne $workspace[0].sid) { throw 'Reinstall drops named workspace accounts.' }
    $existingWorkspaceAgents = @()
    Invoke-Expression $selection.Extent.Text
    if ($workspaceAgents -isnot [array] -or $workspaceAgents.Count) { throw 'Empty installation creates a null workspace account.' }
}
foreach ($bad in @(
    [pscustomobject]@{ workspaceAgents = $workspace[0] },
    [pscustomobject]@{ workspaceAgents = @([pscustomobject]@{ user = $WorkspaceAgentUser; sid = $clientSid }) },
    [pscustomobject]@{ agentSid = $workspace[0].sid; workspaceAgents = @($workspace) },
    [pscustomobject]@{ serviceAccountSid = $workspace[0].sid; workspaceAgents = @($workspace) }
)) {
    $rejected = $false
    try { Get-TrackerWorkspaceAgents $bad | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid workspace account binding was accepted.' }
}
$base = Join-Path $repo ('.runtime\tests\workspace-agents\' + [guid]::NewGuid().ToString('N'))
$tasks = Join-Path $base 'tasks'
$task = Join-Path $tasks '2026\TEST-1'
$content = Join-Path $task 'update'
$private = Join-Path $task '.protected'
$nestedPrivate = Join-Path $content '.protected'
$snapshot = Join-Path $private 'snapshots'
New-Item -ItemType Directory -Path $content, $snapshot, $nestedPrivate -Force | Out-Null
$marker = Join-Path $content 'ordinary.txt'
[IO.File]::WriteAllText($marker, 'synthetic-ordinary')
[IO.File]::WriteAllText((Join-Path $snapshot 'private.txt'), 'synthetic-private')
[IO.File]::WriteAllText((Join-Path $nestedPrivate 'private.txt'), 'synthetic-private')
$config | Add-Member tasksRoot $tasks
$config | Add-Member trackerRoot $base
$config | Add-Member schemaVersion 2
$config | Add-Member targetUser 'test-client'
$config | Add-Member serviceAccountName $identity.Name
$path = Join-Path $base 'service.json'
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding utf8
$helper = Join-Path $repo 'src\Set-TaskDirectoryAcl.ps1'
& $helper -ConfigPath $path -TaskFolder $task
foreach ($directory in @($tasks, (Split-Path -Parent $task), $task, $content)) {
    $acl = Get-Acl -LiteralPath $directory
    $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.IdentityReference.Value -eq $workspace[0].sid -and $_.AccessControlType -eq 'Allow' })
    if (-not $rules.Count) { throw "Workspace account lacks ordinary access: $directory" }
    if (-not ($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.IdentityReference.Value -eq $clientSid })) { throw 'Existing client access was removed.' }
    $direct = @($rules | Where-Object { $_.PropagationFlags -ne 'InheritOnly' })
    $danger = [Security.AccessControl.FileSystemRights]'Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    if ($direct | Where-Object { $_.FileSystemRights -band $danger }) { throw 'Workspace account can alter directories or their ACL.' }
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $serviceSid) { throw 'Ordinary owner changed.' }
}
foreach ($directory in @($private, $snapshot, $nestedPrivate)) {
    $acl = Get-Acl -LiteralPath $directory
    if (-not $acl.AreAccessRulesProtected) { throw 'Private ACL still inherits.' }
    if ($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.IdentityReference.Value -in @($clientSid, $workspace[0].sid) }) {
        throw 'Client or workspace agent was granted private access.'
    }
}
$before = @{}
foreach ($directory in @($private, $snapshot, $nestedPrivate)) { $before[$directory] = (Get-Acl -LiteralPath $directory).Sddl }
& $helper -ConfigPath $path -TaskFolder $task
foreach ($directory in $before.Keys) {
    if ((Get-Acl -LiteralPath $directory).Sddl -cne $before[$directory]) { throw 'Existing private owner or DACL changed.' }
}
$fileRules = @((Get-Acl -LiteralPath $marker).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
    Where-Object { $_.IdentityReference.Value -eq $workspace[0].sid -and $_.AccessControlType -eq 'Allow' })
if (-not ($fileRules | Where-Object { ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Modify) -eq
    [Security.AccessControl.FileSystemRights]::Modify })) { throw 'Ordinary file is not writable for workspace account.' }
# Права проверяются по собственной строке, в том числе при её изменении.
$config | Add-Member taskAclMatrix @(New-TrackerTaskAclMatrix $config)
$row = $config.taskAclMatrix | Where-Object sid -eq $workspace[0].sid
$row.taskDirectories = 'ReadAndExecute'
$row.taskFiles = 'ReadAndExecute'
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding utf8
& $helper -ConfigPath $path -TaskFolder $task
$actual = @((Get-Acl -LiteralPath $content).GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) |
    Where-Object { $_.IdentityReference.Value -eq $workspace[0].sid })
if ($actual | Where-Object { $_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::WriteData }) {
    throw 'Account-specific read-only matrix row was not applied.'
}
$reinstalled = @(New-TrackerTaskAclMatrix $config $config)
$savedRow = $reinstalled | Where-Object sid -eq $workspace[0].sid
if ($savedRow.taskDirectories -cne 'ReadAndExecute' -or $savedRow.taskFiles -cne 'ReadAndExecute') {
    throw 'Reinstall widened the configured account rights.'
}
$row.protectedDirectories = 'ReadAndExecute'
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding utf8
$ordinaryBefore = (Get-Acl -LiteralPath $content).Sddl
$rejected = $false
try { & $helper -ConfigPath $path -TaskFolder $task } catch { $rejected = $true }
if (-not $rejected -or (Get-Acl -LiteralPath $content).Sddl -cne $ordinaryBefore) {
    throw 'Invalid matrix was not rejected before ACL changes.'
}
$config.PSObject.Properties.Remove('taskAclMatrix')
$config.workspaceAgents = @()
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding utf8
& $helper -ConfigPath $path -TaskFolder $task
if ((Get-Acl -LiteralPath $content).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
    Where-Object { $_.IdentityReference.Value -eq $workspace[0].sid }) { throw 'Removed workspace account retained its grant.' }
foreach ($directory in $before.Keys) {
    if ((Get-Acl -LiteralPath $directory).Sddl -cne $before[$directory]) { throw 'Private ACL changed during workspace removal.' }
}
# Оставляем fixtures и возвращаем рабочий grant для отдельной контейнерной проверки.
$config.workspaceAgents = @($workspace)
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding utf8
& $helper -ConfigPath $path -TaskFolder $task
@{ status = 'passed'; fixture = $base; workspaceUser = $workspace[0].user; workspaceSid = $workspace[0].sid;
    checks = @('named-binding', 'old-config', 'invalid-config', 'ordinary-access', 'directory-boundary',
               'nested-private', 'private-dacl-preserved', 'repeat-apply', 'workspace-removal') } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $base 'report.json') -Encoding utf8
Write-Output "Workspace account policy passed; fixture: $base"
