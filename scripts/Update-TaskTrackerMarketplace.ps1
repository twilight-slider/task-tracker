param([Parameter(Mandatory)][string]$TargetUser)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'TaskTracker-AdminCommon.ps1')
. (Join-Path $PSScriptRoot 'TaskTracker-TokenCore.ps1')
. (Join-Path $PSScriptRoot 'TaskTracker-Marketplace.ps1')
$settings = Get-TargetTrackerSettings -TargetUser $TargetUser
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($currentSid -cne $settings.TargetSid) { throw 'Run marketplace update as TargetUser.' }
$envText = Read-TrackerUtf8File $settings.EnvPath
$token = Get-TrackerEnvToken -EnvText $envText
$Port = Get-TrackerEnvPort -EnvText $envText
if ([string]::IsNullOrWhiteSpace($token)) { throw 'TargetUser env.txt has no TRACKER_MCP_TOKEN.' }
$env:TRACKER_MCP_TOKEN = $token
$codex = (Get-Command codex -ErrorAction Stop).Source
$existingMarketplaces = & $codex plugin marketplace list --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Codex marketplace list failed before update.' }
if ((Get-TrackerMarketplaceRegistrationAction -MarketplaceList $existingMarketplaces) -ceq 'add') {
    & $codex plugin marketplace add 'https://github.com/twilight-slider/ai-marketplace.git' --json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Codex could not add the published ai-marketplace Git source.' }
}
& $codex plugin marketplace upgrade ai-marketplace --json | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Codex Git marketplace upgrade failed.' }
$marketplaces = & $codex plugin marketplace list --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Codex marketplace list failed.' }
$marketplace = @($marketplaces.marketplaces | Where-Object { $_.name -ceq 'ai-marketplace' })
if ((Get-TrackerMarketplaceRegistrationAction -MarketplaceList $marketplaces) -cne 'upgrade') {
    throw 'Codex ai-marketplace was not registered after update.'
}
Assert-TrackerPublishedMarketplace -Root ([string]$marketplace[0].root)
$plugins = & $codex plugin list --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Codex plugin list failed.' }
foreach ($entry in @(@{ Name = 'task-folder-workflow'; Version = '0.6.2' },
        @{ Name = 'task-tracker-mcp'; Version = '0.2.2' })) {
    $installed = @($plugins.installed | Where-Object { $_.pluginId -ceq "$($entry.Name)@ai-marketplace" })
    if ($installed.Count -ne 1 -or $installed[0].version -cne $entry.Version -or -not $installed[0].enabled) {
        & $codex plugin add "$($entry.Name)@ai-marketplace" --json | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Codex could not install $($entry.Name)." }
    }
}
$plugins = & $codex plugin list --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Codex plugin list failed after update.' }
Assert-TrackerMarketplaceInstalled -PluginList $plugins
foreach ($scope in @('folders', 'snapshots')) {
    $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/mcp/$scope" -Method Post `
        -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' `
        -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -TimeoutSec 5
    if ($response.jsonrpc -cne '2.0' -or @($response.result.tools).Count -eq 0) {
        throw "MCP $scope tools are unavailable after marketplace update."
    }
}
Write-Output 'Published ai-marketplace and both installed MCP plugins verified for TargetUser.'
