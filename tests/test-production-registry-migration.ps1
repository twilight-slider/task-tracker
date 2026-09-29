$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) "task-folder-registry-$PID"
$source = Join-Path $root 'source.json'
$destination = Join-Path $root 'protected.json'
$script = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\Initialize-ProductionRegistry.ps1'
try {
    New-Item -ItemType Directory -Path $root -ErrorAction Stop | Out-Null
    $manifest = @{ schema_version = 1; projects = @(@{
        project_key = 'AIDEV'; source_type = 'JIRA_CLOUD'; jira_host = 'https://twilight-slider.atlassian.net'
    }) }
    [IO.File]::WriteAllText($source, ($manifest | ConvertTo-Json -Depth 4))
    & $script -SourcePath $source -DestinationPath $destination | Out-Null
    $hash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    & $script -SourcePath $source -DestinationPath $destination | Out-Null
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne $hash) {
        throw 'Repeated migration modified the protected registry.'
    }
    Remove-Item -LiteralPath $destination -Force
    $manifest.projects[0].jira_host = 'https://attacker.invalid'
    [IO.File]::WriteAllText($source, ($manifest | ConvertTo-Json -Depth 4))
    $rejected = $false
    try { & $script -SourcePath $source -DestinationPath $destination | Out-Null }
    catch { $rejected = $true }
    if (-not $rejected -or (Test-Path -LiteralPath $destination)) { throw 'Unexpected project registry was accepted.' }
    Write-Output 'Production registry migration tests passed'
} finally {
    if (Test-Path -LiteralPath $source) { Remove-Item -LiteralPath $source -Force }
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Force }
}
