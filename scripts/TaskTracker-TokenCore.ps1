function Get-TrackerEnvToken([string]$EnvText) {
    $lines = @($EnvText -split "`r?`n" | Where-Object { $_ -match '^\s*TRACKER_MCP_TOKEN\s*=' })
    if ($lines.Count -gt 1) { throw 'TargetUser env.txt has duplicate TRACKER_MCP_TOKEN entries.' }
    if ($lines.Count -eq 0) { return $null }
    return ($lines[0] -split '=', 2)[1].Trim()
}

function Assert-TrackerTokenPair([string]$ProtectedToken, [string]$EnvToken) {
    if ([bool]$ProtectedToken -ne [bool]$EnvToken -or
        ($ProtectedToken -and $ProtectedToken -cne $EnvToken)) {
        throw 'MCP token copies disagree or one is missing; stop reinstall and repair both copies explicitly.'
    }
}

function New-TrackerMcpToken {
    $random = [byte[]]::new(32)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($random) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($random).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Set-TrackerEnvToken([string]$EnvText, [string]$Token) {
    Get-TrackerEnvToken -EnvText $EnvText | Out-Null
    if ($EnvText -match '(?m)^\s*TRACKER_MCP_TOKEN\s*=') {
        return [regex]::Replace($EnvText, '(?m)^\s*TRACKER_MCP_TOKEN\s*=.*$', "TRACKER_MCP_TOKEN=$Token")
    }
    return $EnvText.TrimEnd("`r", "`n") + "`nTRACKER_MCP_TOKEN=$Token`n"
}
