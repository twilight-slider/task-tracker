param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [switch]$Rotate
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1')
. (Join-Path $PSScriptRoot 'TaskTracker-TokenCore.ps1')
Assert-TrackerAdministrator
if (-not (Test-AbsoluteWindowsPath $ConfigPath)) { throw 'ConfigPath must be absolute.' }
$config = Read-TrackerUtf8File $ConfigPath | ConvertFrom-Json
if ($config.schemaVersion -ne 3) { throw 'Bootstrap JSON v3 is required for token operation.' }
$installedServiceSid = Get-InstalledTrackerServiceSid -TargetSid ([string]$config.ownerSid)
Assert-TrackerProtectedFile -Path $ConfigPath -ServiceSid $installedServiceSid | Out-Null
$settings = Get-TargetTrackerSettings -TargetUser ([string]$config.targetUser)
if ($settings.TargetSid -cne [string]$config.ownerSid -or $settings.TrackerRoot -ine [string]$config.trackerRoot) {
    throw 'TargetUser env differs from bootstrap JSON.'
}
if ($installedServiceSid -and $installedServiceSid -cne [string]$config.serviceAccountSid) {
    throw 'Installed service SID differs from bootstrap JSON.'
}
$protected = Assert-TrackerProtectedArea -TrackerRoot $settings.TrackerRoot -TargetSid $settings.TargetSid -ServiceSid $installedServiceSid
$protectedToken = Join-Path $protected 'mcp-token'
$envFile = $settings.EnvPath
$envText = Read-TrackerUtf8File $envFile
$envToken = Get-TrackerEnvToken -EnvText $envText
$protectedExists = Test-Path -LiteralPath $protectedToken -PathType Leaf
if ($protectedExists) {
    $item = Get-Item -LiteralPath $protectedToken -Force
    Assert-TrackerProtectedFile -Path $protectedToken -ServiceSid ([string]$config.serviceAccountSid) | Out-Null
}
$protectedValue = if ($protectedExists) { (Read-TrackerUtf8File $protectedToken).Trim() } else { $null }
Assert-TrackerTokenPair -ProtectedToken $protectedValue -EnvToken $envToken
if ($Rotate -and -not $protectedValue) { throw 'Cannot rotate an absent MCP token; run initial installation first.' }
if ($protectedValue -and -not $Rotate) {
    Write-Output 'MCP token preserved and both copies match.'
    return
}
$newToken = New-TrackerMcpToken
$newEnv = Set-TrackerEnvToken -EnvText $envText -Token $newToken
$service = $null
$port = 0
if ($Rotate) {
    $installedPath = Join-Path $protected 'service.json'
    Assert-TrackerProtectedFile -Path $installedPath -ServiceSid ([string]$config.serviceAccountSid) | Out-Null
    $installed = Read-TrackerUtf8File $installedPath | ConvertFrom-Json
    if ($installed.workerLanguage -cne 'python' -or [int]$installed.mcpPort -lt 1 -or
        [string]$installed.serviceName -cne [string]$config.serviceName) {
        throw 'Installed Python MCP service configuration is required for token rotation.'
    }
    $port = [int]$installed.mcpPort
    $service = Get-Service -Name ([string]$config.serviceName) -ErrorAction Stop
    if ($service.Status -ne 'Running') { throw 'MCP service must be Running before token rotation.' }
}
function Assert-McpTokenActive([int]$Port, [string]$Token) {
    $uri = "http://127.0.0.1:$Port/mcp/folders"
    $body = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        try {
            $response = Invoke-RestMethod -Uri $uri -Method Post -Headers @{ Authorization = "Bearer $Token" } `
                -ContentType 'application/json' -Body $body -TimeoutSec 3
            if ($response.jsonrpc -ceq '2.0' -and $null -ne $response.result) { return }
        } catch { }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'MCP endpoint did not accept the token after service restart.'
}
$tempToken = Join-Path $protected ('.mcp-token-' + [guid]::NewGuid().ToString('N') + '.tmp')
$tempEnv = Join-Path (Split-Path -Parent $envFile) ('.env-token-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    [IO.File]::WriteAllText($tempToken, "$newToken`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($tempEnv, $newEnv, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempToken -Destination $protectedToken -Force
    Move-Item -LiteralPath $tempEnv -Destination $envFile -Force
    $savedLines = @([IO.File]::ReadAllLines($envFile) | Where-Object { $_ -match '^\s*TRACKER_MCP_TOKEN\s*=' })
    if ([IO.File]::ReadAllText($protectedToken).Trim() -cne $newToken -or
        $savedLines.Count -ne 1 -or ($savedLines[0] -split '=', 2)[1].Trim() -cne $newToken) {
        throw 'MCP token verification failed.'
    }
    if ($Rotate) {
        Restart-Service -Name $service.Name -ErrorAction Stop
        Assert-McpTokenActive -Port $port -Token $newToken
    }
} catch {
    $reason = $_.Exception.Message
    try {
        if ($protectedExists) { [IO.File]::WriteAllText($protectedToken, "$protectedValue`n", [Text.UTF8Encoding]::new($false)) }
        elseif (Test-Path -LiteralPath $protectedToken) { Remove-Item -LiteralPath $protectedToken }
        [IO.File]::WriteAllText($envFile, $envText, [Text.UTF8Encoding]::new($false))
        if ($Rotate) {
            Restart-Service -Name $service.Name -ErrorAction Stop
            Assert-McpTokenActive -Port $port -Token $protectedValue
        }
    } catch { throw "RECOVERY_INCOMPLETE: MCP token rollback failed after $reason" }
    throw "MCP token update rolled back: $reason"
} finally {
    foreach ($temp in @($tempToken, $tempEnv)) { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp } }
}
Write-Output $(if ($Rotate) { 'MCP token rotated in both copies.' } else { 'MCP token created in both copies.' })
