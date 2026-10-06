function Assert-TrackerPublishedMarketplace([string]$Root, [int]$Port) {
    $expected = @(
        @{ Name = 'task-folder-workflow'; Version = '0.6.0'; Scope = 'folders'; Tools = @('get_tasks_folder', 'create_task_folder') },
        @{ Name = 'task-tracker-mcp'; Version = '0.2.0'; Scope = 'snapshots'; Tools = @('create_result_snapshot', 'compare_result_snapshot') }
    )
    foreach ($entry in $expected) {
        $plugin = Join-Path $Root "plugins\$($entry.Name)"
        $manifest = Join-Path $plugin '.codex-plugin\plugin.json'
        $mcp = Join-Path $plugin '.mcp.json'
        if (-not (Test-Path -LiteralPath $manifest -PathType Leaf) -or -not (Test-Path -LiteralPath $mcp -PathType Leaf)) {
            throw "Published marketplace lacks $($entry.Name). Publish the new ai-marketplace version first."
        }
        $meta = [IO.File]::ReadAllText($manifest, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
        $server = ([IO.File]::ReadAllText($mcp, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json).mcpServers.($entry.Name)
        if ($meta.version -cne $entry.Version -or $server.type -cne 'http' -or
            $server.url -cne "http://127.0.0.1:$Port/mcp/$($entry.Scope)" -or
            $server.bearer_token_env_var -cne 'TRACKER_MCP_TOKEN' -or
            $server.command -or $server.args) {
            throw "Published $($entry.Name) does not match the direct MCP release. Publish the new ai-marketplace version first."
        }
        foreach ($tool in $entry.Tools) {
            if ($tool -cnotin @($server.enabled_tools)) { throw "Published $($entry.Name) lacks $tool." }
        }
    }
}

function Assert-TrackerMarketplaceInstalled([object]$PluginList) {
    foreach ($entry in @(@{ Name = 'task-folder-workflow'; Version = '0.6.0' },
            @{ Name = 'task-tracker-mcp'; Version = '0.2.0' })) {
        $plugin = @($PluginList.installed | Where-Object { $_.pluginId -ceq "$($entry.Name)@ai-marketplace" })
        if ($plugin.Count -ne 1 -or $plugin[0].version -cne $entry.Version -or -not $plugin[0].enabled) {
            throw "Codex has not installed enabled $($entry.Name) $($entry.Version)."
        }
    }
}

function Get-TrackerMarketplaceRegistrationAction([object]$MarketplaceList) {
    $matches = @($MarketplaceList.marketplaces | Where-Object { $_.name -ceq 'ai-marketplace' })
    if ($matches.Count -eq 0) { return 'add' }
    if ($matches.Count -ne 1 -or $matches[0].marketplaceSource.sourceType -cne 'git' -or
        $matches[0].marketplaceSource.source -cne 'https://github.com/twilight-slider/ai-marketplace.git') {
        throw 'Codex ai-marketplace is not the expected Git source.'
    }
    return 'upgrade'
}
