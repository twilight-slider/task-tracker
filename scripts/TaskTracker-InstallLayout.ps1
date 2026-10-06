function Remove-TrackerInstallTree([string]$ProtectedRoot, [string]$Path) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $base = [IO.Path]::GetFullPath($ProtectedRoot).TrimEnd('\') + '\'
    if (-not $full.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside protected Tracker: $Path"
    }
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

function Test-TrackerHostReuse($PriorConfig, [string]$SourceHash, [string]$ExistingExe) {
    return ($null -ne $PriorConfig -and
        [string]$PriorConfig.hostSourceSha256 -ceq $SourceHash -and
        (Test-Path -LiteralPath $ExistingExe -PathType Leaf))
}

function Assert-TrackerSnapshotRoots([string[]]$Roots, [string]$TrackerRoot) {
    if (-not $Roots -or $Roots.Count -eq 0) { throw 'At least one snapshot root is required.' }
    $tracker = [IO.Path]::GetFullPath($TrackerRoot).TrimEnd('\')
    $verified = foreach ($root in $Roots) {
        $full = Assert-PlainDirectory ([string]$root)
        $full = $full.TrimEnd('\')
        if ($tracker.Equals($full, [StringComparison]::OrdinalIgnoreCase) -or
            $tracker.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($tracker + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Snapshot root must be separate from Tracker storage: $full"
        }
        $full
    }
    return @($verified | Select-Object -Unique)
}

function Get-TrackerTokenRollbackState([string]$ProtectedTokenPath, [string]$EnvPath) {
    $exists = Test-Path -LiteralPath $ProtectedTokenPath -PathType Leaf
    return [pscustomobject]@{
        ProtectedTokenPath = $ProtectedTokenPath
        ProtectedTokenExists = $exists
        ProtectedTokenText = if ($exists) { [IO.File]::ReadAllText($ProtectedTokenPath, [Text.UTF8Encoding]::new($false, $true)) } else { $null }
        EnvPath = $EnvPath
        EnvText = [IO.File]::ReadAllText($EnvPath, [Text.UTF8Encoding]::new($false, $true))
        EnvAcl = Get-Acl -LiteralPath $EnvPath
    }
}

function Restore-TrackerTokenPair($State) {
    if ($State.ProtectedTokenExists) {
        [IO.File]::WriteAllText($State.ProtectedTokenPath, $State.ProtectedTokenText, [Text.UTF8Encoding]::new($false))
    } elseif (Test-Path -LiteralPath $State.ProtectedTokenPath) {
        Remove-Item -LiteralPath $State.ProtectedTokenPath -ErrorAction Stop
    }
    [IO.File]::WriteAllText($State.EnvPath, $State.EnvText, [Text.UTF8Encoding]::new($false))
    $currentAcl = Get-Acl -LiteralPath $State.EnvPath
    if ($currentAcl.Sddl -cne $State.EnvAcl.Sddl) {
        Set-Acl -LiteralPath $State.EnvPath -AclObject $State.EnvAcl -ErrorAction Stop
    }
}

function Restore-TrackerInstallLayout([string]$ProtectedRoot, [string]$BackupRoot,
    [hashtable]$HadLive, $OldConfig) {
    foreach ($name in @('bin', 'runtime')) {
        $live = Join-Path $ProtectedRoot $name
        $previous = Join-Path $BackupRoot $name
        if (Test-Path -LiteralPath $previous) {
            Remove-TrackerInstallTree -ProtectedRoot $ProtectedRoot -Path $live
            Move-Item -LiteralPath $previous -Destination $live
        } elseif (-not $HadLive[$name]) {
            Remove-TrackerInstallTree -ProtectedRoot $ProtectedRoot -Path $live
        }
    }
    $configPath = Join-Path $ProtectedRoot 'service.json'
    if ($null -ne $OldConfig) {
        [IO.File]::WriteAllText($configPath, $OldConfig, [Text.UTF8Encoding]::new($false))
    } elseif (Test-Path -LiteralPath $configPath) {
        Remove-Item -LiteralPath $configPath
    }
}
