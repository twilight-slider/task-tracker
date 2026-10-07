function Assert-TrackerAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this TaskTracker step from an elevated PowerShell window.'
    }
}

function Test-AbsoluteWindowsPath([string]$Path) {
    return $Path -match '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+(?:[\\/]|$))'
}

function Read-TrackerUtf8File([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false, $true))
}

function Assert-PlainDirectory([string]$Path) {
    if (-not (Test-AbsoluteWindowsPath $Path)) { throw "Directory path must be absolute: $Path" }
    $full = [IO.Path]::GetFullPath($Path)
    $current = [IO.Path]::GetPathRoot($full)
    foreach ($part in $full.Substring($current.Length).Split([IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Directory path contains a file or reparse point: $current"
        }
    }
    return $full
}

function Read-TrackerFolderFromEnv([string]$EnvPath) {
    if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        throw "TargetUser env file is missing: $EnvPath. Create it with one TRACKER_FOLDER=<absolute dedicated path> and rerun."
    }
    $item = Get-Item -LiteralPath $EnvPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "TargetUser env file must be plain: $EnvPath" }
    $matches = @((Read-TrackerUtf8File $EnvPath) -split "`r?`n" | Where-Object { $_ -match '^\s*TRACKER_FOLDER\s*=' })
    if ($matches.Count -ne 1) { throw "$EnvPath must contain exactly one TRACKER_FOLDER=<absolute dedicated path>." }
    $raw = ($matches[0] -split '=', 2)[1].Trim()
    if (-not (Test-AbsoluteWindowsPath $raw)) { throw "TRACKER_FOLDER must be an absolute path in $EnvPath." }
    $tracker = [IO.Path]::GetFullPath($raw).TrimEnd('\')
    if ($tracker -eq [IO.Path]::GetPathRoot($tracker).TrimEnd('\')) { throw 'TRACKER_FOLDER cannot be a volume root.' }
    return $tracker
}

function Read-GitFolderFromEnv([string]$EnvPath) {
    $matches = @((Read-TrackerUtf8File $EnvPath) -split "`r?`n" | Where-Object { $_ -match '^\s*GIT_FOLDER\s*=' })
    if ($matches.Count -ne 1) { throw "$EnvPath must contain exactly one GIT_FOLDER=<absolute project root>." }
    $raw = ($matches[0] -split '=', 2)[1].Trim()
    if (-not (Test-AbsoluteWindowsPath $raw)) { throw "GIT_FOLDER must be an absolute path in $EnvPath." }
    return Assert-PlainDirectory $raw
}

function Get-TargetTrackerSettings([string]$TargetUser) {
    if ([string]::IsNullOrWhiteSpace($TargetUser)) { throw 'Specify -TargetUser.' }
    try { $sid = ([Security.Principal.NTAccount]$TargetUser).Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { throw "Cannot resolve TargetUser '$TargetUser' to a Windows SID." }
    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"
    $profile = (Get-ItemProperty -LiteralPath $profileKey -ErrorAction Stop).ProfileImagePath
    $profile = [Environment]::ExpandEnvironmentVariables([string]$profile)
    if (-not (Test-AbsoluteWindowsPath $profile)) { throw "TargetUser profile is invalid: $profile" }
    $profile = Assert-PlainDirectory $profile
    Assert-PlainDirectory (Join-Path $profile '.env') | Out-Null
    $envFile = Join-Path $profile '.env\env.txt'
    $tracker = Read-TrackerFolderFromEnv -EnvPath $envFile
    [pscustomobject]@{ TargetUser = $TargetUser; TargetSid = $sid; Profile = $profile; EnvPath = $envFile; TrackerRoot = $tracker }
}

function Assert-TaskTrackerCheckout([string]$RepositoryRoot, [string]$ExpectedCommit) {
    $repository = Assert-PlainDirectory $RepositoryRoot
    $git = (Get-Command git.exe -ErrorAction Stop).Source
    $safeDirectory = $repository.Replace('\', '/')
    function Read-Git([string[]]$Arguments) {
        $output = & $git -c "safe.directory=$safeDirectory" -C $repository @Arguments 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Git check failed: git $($Arguments -join ' ')`n$(($output | Out-String).Trim())" }
        return (($output | Out-String).Trim())
    }
    $top = Read-Git -Arguments @('rev-parse', '--show-toplevel')
    if ([IO.Path]::GetFullPath($top).TrimEnd('\') -ine $repository.TrimEnd('\')) { throw 'Git checkout root differs from selected repository.' }
    $branch = Read-Git -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD')
    if ($branch -cne 'main') { throw "TaskTracker checkout must be on main; current branch: $branch" }
    $head = Read-Git -Arguments @('rev-parse', '--verify', 'HEAD')
    $origin = Read-Git -Arguments @('rev-parse', '--verify', 'refs/remotes/origin/main')
    if ($head -cne $origin) { throw 'Local HEAD differs from local origin/main. Synchronize main before running bootstrap/wrapper.' }
    if ($ExpectedCommit -and $head -cne $ExpectedCommit) { throw 'Checkout commit differs from bootstrap JSON. Rerun administrative bootstrap.' }
    $status = Read-Git -Arguments @('status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
    if ($status) { throw "TaskTracker checkout has staged, unstaged or untracked changes. Commit/clean them before bootstrap/wrapper.`n$status" }
    return $head
}

function Assert-TrackerProtectedArea([string]$TrackerRoot, [string]$TargetSid, [string]$ServiceSid) {
    $tracker = Assert-PlainDirectory $TrackerRoot
    $protected = Assert-PlainDirectory (Join-Path $tracker '.protected')
    $acl = Get-Acl -LiteralPath $protected
    if (-not $acl.AreAccessRulesProtected) { throw "Protected Tracker ACL still inherits permissions: $protected" }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $trusted = @('S-1-5-18', 'S-1-5-32-544')
    if ($ServiceSid) { $trusted += $ServiceSid }
    if ($owner -notin $trusted) {
        throw "Protected Tracker owner must be SYSTEM, Administrators or service account: $protected; owner $owner"
    }
    $write = [Security.AccessControl.FileSystemRights]'Write, Modify, FullControl, Delete, ChangePermissions, TakeOwnership'
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne 'Allow' -or $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $trusted -and ($rule.FileSystemRights -band $write)) {
            throw "An untrusted account can write protected Tracker area: $protected ($sid)"
        }
    }
    return $protected
}

function Assert-TrackerProtectedFile([string]$Path, [string]$ServiceSid) {
    $file = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Protected file must be plain: $Path"
    }
    $trusted = @('S-1-5-18', 'S-1-5-32-544')
    if ($ServiceSid) { $trusted += $ServiceSid }
    $acl = Get-Acl -LiteralPath $Path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $trusted) { throw "Protected file has untrusted owner: $Path ($owner)" }
    $write = [Security.AccessControl.FileSystemRights]'Write, Modify, FullControl, Delete, ChangePermissions, TakeOwnership'
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne 'Allow' -or $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $trusted -and ($rule.FileSystemRights -band $write)) {
            throw "Protected file permits untrusted write: $Path ($sid)"
        }
    }
    return $Path
}

function Get-InstalledTrackerServiceSid([string]$TargetSid) {
    $rid = $TargetSid.Split('-')[-1]
    $serviceName = "TaskTracker-$rid"
    $service = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
    if (-not $service) { return $null }
    $accountName = ([string]$service.StartName) -replace '^\.(?=\\)', [Environment]::MachineName
    try { return ([Security.Principal.NTAccount]$accountName).Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { throw "Cannot resolve installed service account for $serviceName." }
}

function Test-TrustedTrackerBoundarySid([string]$Sid, [string]$ServiceSid,
    [bool]$TrustAuthenticatedUsers) {
    if ($Sid -in @('S-1-5-18', 'S-1-5-32-544') -or ($ServiceSid -and $Sid -eq $ServiceSid)) { return $true }
    if ($TrustAuthenticatedUsers -and ($Sid -eq 'S-1-5-11' -or $Sid -match '^S-1-5-21-(?:[0-9]+-){3}[0-9]+$')) {
        return $true
    }
    return $false
}

function Assert-TrackerRootBoundary([string]$TrackerRoot, [string]$TargetSid, [string]$ServiceSid,
    [bool]$TrustAuthenticatedUsers = $false) {
    $root = Assert-PlainDirectory $TrackerRoot
    $parent = Assert-PlainDirectory ([IO.Path]::GetDirectoryName($root))
    $trusted = @('S-1-5-18', 'S-1-5-32-544')
    if ($ServiceSid) { $trusted += $ServiceSid }
    $parentDanger = [Security.AccessControl.FileSystemRights]'DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    $rootDanger = [Security.AccessControl.FileSystemRights]'CreateFiles, CreateDirectories, WriteData, AppendData, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    foreach ($path in @($parent, $root)) {
        $acl = Get-Acl -LiteralPath $path
        if ($path -ieq $root -and -not $acl.AreAccessRulesProtected) {
            throw "Tracker root must not inherit parent permissions: $root"
        }
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        $allowedOwners = if ($path -ieq $parent) { @('S-1-5-18', 'S-1-5-32-544') } else { $trusted }
        if ($owner -notin $allowedOwners) { throw "Untrusted Tracker boundary owner: $path ($owner)" }
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne 'Allow' -or $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            $trustedForPath = if ($path -ieq $parent) {
                Test-TrustedTrackerBoundarySid -Sid $sid -ServiceSid $ServiceSid -TrustAuthenticatedUsers $TrustAuthenticatedUsers
            } else { $sid -in $trusted }
            $danger = if ($path -ieq $parent) { $parentDanger } else { $rootDanger }
            if (-not $trustedForPath -and ($rule.FileSystemRights -band $danger)) {
                throw "Untrusted account can alter Tracker boundary: $path ($sid)"
            }
        }
    }
    if ($TargetSid -in $trusted) { throw 'TargetUser cannot be the service account or a trusted administrator.' }
    return $root
}
