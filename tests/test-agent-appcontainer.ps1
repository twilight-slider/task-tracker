param(
    [Parameter(Mandatory)][string]$AgentAccountName,
    [Parameter(Mandatory)][string]$CodexExecutable,
    [switch]$PreflightOnly,
    [switch]$RealTurn
)
$ErrorActionPreference = 'Stop'
if ($AgentAccountName -notmatch '^[\p{L}\p{N}._-]{1,20}$') { throw 'Invalid local agent account name.' }
$account = Get-LocalUser -Name $AgentAccountName -ErrorAction Stop
if (-not $account.Enabled) { throw 'Agent account is disabled.' }
if (-not (Test-Path -LiteralPath $CodexExecutable -PathType Leaf)) { throw 'Codex executable not found.' }
if ($RealTurn -and -not [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'RealTurn through local Privoxy requires an elevated PowerShell for a temporary AppContainer loopback exemption.'
}
$repo = Split-Path -Parent $PSScriptRoot
$testParent = Join-Path $repo '.runtime\tests\appcontainer-probe'
New-Item -ItemType Directory -Path $testParent -Force | Out-Null
$agentRoot = Join-Path $testParent ('agent-run-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $agentRoot | Out-Null
$loopbackExemptName = $null
$loopbackAdded = $false
$cleanupSafe = $true
try {
    $originalAcl = Get-Acl -LiteralPath $agentRoot
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($originalAcl.GetOwner([Security.Principal.SecurityIdentifier]))
    $agentIdentity = $account.SID.Translate([Security.Principal.NTAccount]).Value
    foreach ($rule in $originalAcl.Access) {
        if ($rule.IdentityReference.Value -in @($account.SID.Value, $agentIdentity)) { continue }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $rule.IdentityReference, $rule.FileSystemRights, $rule.InheritanceFlags,
            $rule.PropagationFlags, $rule.AccessControlType))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $account.SID, [Security.AccessControl.FileSystemRights]::Modify,
        [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow))
    Set-Acl -LiteralPath $agentRoot -AclObject $acl
    $actual = Get-Acl -LiteralPath $agentRoot
    if (-not $actual.AreAccessRulesProtected -or @($actual.Access | Where-Object {
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny -and
        $_.IdentityReference.Value -in @($account.SID.Value, $agentIdentity)
    }).Count -ne 0) { throw 'Temporary test directory still denies the agent account.' }
    if ($PreflightOnly) { Write-Output 'Agent test-directory ACL preflight passed'; return }

    $copy = Join-Path $agentRoot 'codex-agent-test.exe'
    Copy-Item -LiteralPath $CodexExecutable -Destination $copy
    $log = Join-Path $agentRoot 'probe.stdout.txt'
    $errorLog = Join-Path $agentRoot 'probe.stderr.txt'
    $completion = Join-Path $agentRoot 'probe.exitcode.txt'
    $credential = Get-Credential -UserName ".\$AgentAccountName" -Message "Password for local test account $AgentAccountName (not stored)"
    if (-not $credential) { throw 'Agent credential was not supplied.' }
    if ($RealTurn) {
        $loopbackExemptName = 'AIDEV57.A.' + [guid]::NewGuid().ToString('N')
        $netIsolation = Join-Path $env:WINDIR 'System32\CheckNetIsolation.exe'
        & $netIsolation LoopbackExempt -a "-n=$loopbackExemptName" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not add temporary AppContainer loopback exemption.' }
        $loopbackAdded = $true
    }
    $pwsh = (Get-Command pwsh -ErrorAction Stop).Source
    $script = Join-Path $PSScriptRoot 'agent-appcontainer-wrapper.ps1'
    $probeScript = Join-Path $PSScriptRoot 'test-appcontainer-probe.ps1'
    $childRoot = Join-Path $agentRoot 'probe'
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    try {
        $env:TEMP = $agentRoot
        $env:TMP = $agentRoot
        $childArgs = @('-NoProfile', '-File', "`"$script`"", '-ProbeScript', "`"$probeScript`"", `
            '-CodexExecutable', "`"$copy`"", '-TestRoot', "`"$childRoot`"", '-CompletionPath', "`"$completion`"")
        if ($RealTurn) { $childArgs += '-RealTurn' }
        if ($loopbackExemptName) { $childArgs += @('-AppContainerName', "`"$loopbackExemptName`"") }
        $cleanupSafe = $false
        Start-Process -FilePath $pwsh -Credential $credential -LoadUserProfile -WindowStyle Hidden `
            -ArgumentList $childArgs `
            -WorkingDirectory $agentRoot -RedirectStandardOutput $log -RedirectStandardError $errorLog
    } finally {
        $env:TEMP = $previousTemp
        $env:TMP = $previousTmp
    }
    $timeoutSeconds = if ($RealTurn) { 240 } else { 90 }
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    while (-not (Test-Path -LiteralPath $completion) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not (Test-Path -LiteralPath $completion)) { throw "Agent probe did not report completion within $timeoutSeconds seconds." }
    $exitCode = [int](Get-Content -LiteralPath $completion -Raw)
    $cleanupSafe = $true
    $output = Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue
    if ($exitCode -ne 0) {
        $details = Get-Content -LiteralPath $errorLog -Raw -ErrorAction SilentlyContinue
        throw "Agent AppContainer probe failed ($exitCode): $details"
    }
    if ($output -notmatch [regex]::Escape($account.SID.Value)) { throw 'Probe process did not report the expected agent SID.' }
    Write-Output $output.Trim()
} catch {
    $details = if ($errorLog -and (Test-Path -LiteralPath $errorLog)) {
        Get-Content -LiteralPath $errorLog -Raw -ErrorAction SilentlyContinue
    }
    if ($details) { throw "$($_.Exception.Message) Child stderr: $details" }
    throw
} finally {
    $loopbackCleanupFailure = $null
    if ($loopbackAdded) {
        try {
            & $netIsolation LoopbackExempt -d "-n=$loopbackExemptName" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Could not remove temporary AppContainer loopback exemption.' }
        } catch { $loopbackCleanupFailure = $_.Exception.Message }
    }
    $resolvedParent = ([IO.Path]::GetFullPath($testParent)).TrimEnd('\') + '\'
    $resolvedTarget = [IO.Path]::GetFullPath($agentRoot)
    if (-not $resolvedTarget.StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedTarget) -notmatch '^agent-run-[0-9a-f]{32}$') {
        throw 'Unsafe temporary test-directory cleanup target.'
    }
    if ($PreflightOnly -or $cleanupSafe) { if (Test-Path -LiteralPath $resolvedTarget) {
        $item = Get-Item -LiteralPath $resolvedTarget -Force
        $links = @(Get-ChildItem -LiteralPath $resolvedTarget -Recurse -Force |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $links.Count) {
            throw 'Temporary test directory contains a reparse point; cleanup requires manual inspection.'
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            try { Remove-Item -LiteralPath $resolvedTarget -Recurse -Force -ErrorAction Stop; break }
            catch {
                if ([DateTime]::UtcNow -ge $deadline) { throw }
                Start-Sleep -Milliseconds 100
            }
        } while ($true)
    }} else { Write-Warning "Agent probe may still be running; preserved test directory: $resolvedTarget" }
    if ($loopbackCleanupFailure) { throw $loopbackCleanupFailure }
}
