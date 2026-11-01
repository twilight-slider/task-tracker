function Resolve-TrackerWorkspaceAgents([string[]]$Users) {
    $seen = @{}
    foreach ($user in $Users) {
        if ([string]::IsNullOrWhiteSpace($user)) { throw 'Workspace agent account name is empty.' }
        $sid = ([Security.Principal.NTAccount]$user).Translate([Security.Principal.SecurityIdentifier])
        $account = Get-LocalUser -SID $sid -ErrorAction Stop
        if (-not $account.Enabled) { throw "Workspace agent account is disabled: $user" }
        $administrators = Get-LocalGroup -SID 'S-1-5-32-544'
        if (Get-LocalGroupMember -Group $administrators.Name -ErrorAction Stop |
            Where-Object { $_.SID.Value -eq $sid.Value }) {
            throw "Workspace agent must not be an administrator: $user"
        }
        if (-not $seen.ContainsKey($sid.Value)) {
            $seen[$sid.Value] = $true
            [pscustomobject]@{ user = $sid.Translate([Security.Principal.NTAccount]).Value; sid = $sid.Value }
        }
    }
}

function Get-TrackerWorkspaceAgents($Config) {
    $entries = $Config.workspaceAgents
    if ($null -eq $entries) { return }
    if ($entries -isnot [array]) { throw 'workspaceAgents must be an array of account names and SIDs.' }
    $seen = @{}
    foreach ($entry in $entries) {
        if ($entry.user -isnot [string] -or $entry.sid -isnot [string] -or
            $entry.sid -notmatch '^S-1-5-21-(?:[0-9]+-){3}[0-9]+$') {
            throw 'Workspace agent must contain a local account name and SID.'
        }
        $resolved = @(Resolve-TrackerWorkspaceAgents -Users @($entry.user))
        if ($resolved.Count -ne 1 -or $resolved[0].sid -cne $entry.sid -or
            $entry.sid -in @($Config.agentSid, $Config.serviceAccountSid)) {
            throw "Workspace account binding is invalid: $($entry.user)"
        }
        if (-not $seen.ContainsKey($entry.sid)) {
            $seen[$entry.sid] = $true
            $resolved[0]
        }
    }
}

# Каждая строка хранит собственные права; роли не копируют ACL друг друга.
function New-TrackerTaskAclMatrix($Config, $PreviousConfig = $null) {
    $previous = if ($PreviousConfig) { @(Get-TrackerTaskAclMatrix $PreviousConfig) } else { @() }
    $accounts = @(
        @{ user = 'SYSTEM'; sid = 'S-1-5-18'; trusted = $true },
        @{ user = 'BUILTIN\Administrators'; sid = 'S-1-5-32-544'; trusted = $true },
        @{ user = $Config.serviceAccountName; sid = $Config.serviceAccountSid; trusted = $true },
        @{ user = $Config.targetUser; sid = $Config.agentSid; trusted = $false }
    )
    foreach ($entry in @(Get-TrackerWorkspaceAgents $Config)) {
        $accounts += @{ user = $entry.user; sid = $entry.sid; trusted = $false }
    }
    foreach ($account in $accounts) {
        $row = [pscustomobject]@{
            user = $account.user; sid = $account.sid
            treeDirectories = $(if ($account.trusted) { 'FullControl' } else { 'ReadAndExecute' })
            taskDirectories = $(if ($account.trusted) { 'FullControl' } else { 'ReadAndExecute, CreateFiles' })
            taskFiles = 'FullControl'
            protectedDirectories = $(if ($account.trusted) { 'FullControl' } else { $null })
        }
        $saved = @($previous | Where-Object sid -eq $account.sid)
        if ($saved.Count -eq 1) {
            foreach ($column in @('treeDirectories', 'taskDirectories', 'taskFiles', 'protectedDirectories')) {
                $row.$column = $saved[0].$column
            }
        }
        $row
    }
}

function Get-TrackerTaskAclMatrix($Config) {
    $defaults = @(New-TrackerTaskAclMatrix $Config)
    $rows = if ($null -eq $Config.taskAclMatrix) { $defaults } else { $Config.taskAclMatrix }
    if ($rows -isnot [array] -or $rows.Count -ne $defaults.Count) { throw 'Invalid taskAclMatrix account list.' }
    $seen = @{}
    foreach ($row in $rows) {
        $account = @($defaults | Where-Object { $_.sid -eq $row.sid -and $_.user -eq $row.user })
        if ($account.Count -ne 1 -or $seen.ContainsKey([string]$row.sid)) { throw 'Invalid taskAclMatrix account binding.' }
        $seen[[string]$row.sid] = $true
        foreach ($column in @('treeDirectories', 'taskDirectories', 'taskFiles', 'protectedDirectories')) {
            $value = $row.$column
            if ($account[0].protectedDirectories) {
                if ($value -cne 'FullControl') { throw 'Trusted task accounts require FullControl.' }
            } elseif ($column -eq 'protectedDirectories') {
                if ($null -ne $value) { throw 'Workspace accounts cannot access protected directories.' }
            } else {
                if ($value -isnot [string] -or -not $value) { throw 'Task matrix rights must be explicit.' }
                $rights = [Security.AccessControl.FileSystemRights]$value
                $allowed = [Security.AccessControl.FileSystemRights]$account[0].$column
                if (([int]$rights -band (-bnot [int]$allowed)) -ne 0) { throw 'Task matrix rights exceed the directory boundary.' }
            }
        }
        $row
    }
}
