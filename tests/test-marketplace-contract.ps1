param([string]$MarketplaceRoot)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
. (Join-Path $repo 'scripts\TaskTracker-Marketplace.ps1')
$published = if ($MarketplaceRoot) { $MarketplaceRoot } else { Join-Path (Split-Path -Parent $repo) 'ai-marketplace' }
if (-not (Test-Path -LiteralPath $published -PathType Container)) {
    throw "Set -MarketplaceRoot to the canonical ai-marketplace checkout: $published"
}
Assert-TrackerPublishedMarketplace -Root $published -Port 38772
$wrongPort = $false
try { Assert-TrackerPublishedMarketplace -Root $published -Port 38773 } catch { $wrongPort = $true }
if (-not $wrongPort) { throw 'Marketplace contract accepted a wrong MCP port.' }
Assert-TrackerMarketplaceInstalled -PluginList ([pscustomobject]@{ installed = @(
    [pscustomobject]@{ pluginId = 'task-folder-workflow@ai-marketplace'; version = '0.6.1'; enabled = $true },
    [pscustomobject]@{ pluginId = 'task-tracker-mcp@ai-marketplace'; version = '0.2.1'; enabled = $true }
) })
if ((Get-TrackerMarketplaceRegistrationAction -MarketplaceList ([pscustomobject]@{ marketplaces = @() })) -cne 'add') {
    throw 'First-run marketplace registration was not selected.'
}
$source = [pscustomobject]@{ marketplaces = @([pscustomobject]@{
    name = 'ai-marketplace'; marketplaceSource = [pscustomobject]@{
        sourceType = 'git'; source = 'https://github.com/twilight-slider/ai-marketplace.git'
    }
}) }
if ((Get-TrackerMarketplaceRegistrationAction -MarketplaceList $source) -cne 'upgrade') {
    throw 'Existing marketplace upgrade was not selected.'
}
$source.marketplaces[0].marketplaceSource.source = 'https://example.invalid/other.git'
$rejected = $false
try { Get-TrackerMarketplaceRegistrationAction -MarketplaceList $source | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'Conflicting marketplace source was accepted.' }
Write-Output 'Published marketplace contract checks passed'
