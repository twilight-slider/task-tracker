param(
    [Parameter(Mandatory)][string]$SourcePath,
    [Parameter(Mandatory)][string]$DestinationPath
)

$ErrorActionPreference = 'Stop'
function Read-Registry([string]$Path, [bool]$InitialSource) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Project registry is not a regular file: $Path"
    }
    $manifest = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or @($manifest.projects).Count -lt 1 -or
        ($InitialSource -and @($manifest.projects).Count -ne 1)) {
        throw "Invalid production project registry: $Path"
    }
    $project = @($manifest.projects | Where-Object project_key -EQ 'AIDEV')
    if ($project.Count -ne 1) { throw "AIDEV registration is missing or duplicated: $Path" }
    $project = $project[0]
    if ($project.project_key -ne 'AIDEV' -or $project.source_type -ne 'JIRA_CLOUD' -or
        $project.jira_host -ne 'https://twilight-slider.atlassian.net') {
        throw "Unexpected production project registration: $Path"
    }
    return $manifest
}

if (Test-Path -LiteralPath $DestinationPath) {
    Read-Registry $DestinationPath $false | Out-Null
    Write-Output "Already initialized: $DestinationPath"
    return
}
Read-Registry $SourcePath $true | Out-Null
$text = [IO.File]::ReadAllText($SourcePath)
[IO.File]::WriteAllText($DestinationPath, $text, [Text.UTF8Encoding]::new($false))
Write-Output "Migrated project registry: $DestinationPath"
