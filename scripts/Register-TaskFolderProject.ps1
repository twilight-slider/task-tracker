param(
    [Parameter(Mandatory)][string]$ProjectKey,
    [Parameter(Mandatory)][ValidateSet('NO_JIRA', 'JIRA_SERVER', 'JIRA_CLOUD')][string]$SourceType,
    [string]$Name,
    [string]$JiraHost,
    [int]$NextIssueNumber = 1
)

$ErrorActionPreference = 'Stop'
if ($ProjectKey -cnotmatch '^[A-Z][A-Z0-9_-]*$') { throw 'ProjectKey must be uppercase.' }
if ($SourceType -eq 'NO_JIRA' -and $NextIssueNumber -lt 1) { throw 'NextIssueNumber must be positive.' }
if ($SourceType -ne 'NO_JIRA') {
    $uri = $null
    if (-not [Uri]::TryCreate($JiraHost, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne 'https' -or $uri.AbsoluteUri -ne ($uri.GetLeftPart([UriPartial]::Authority) + '/')) {
        throw 'JiraHost must be one HTTPS origin without a path.'
    }
    $JiraHost = $uri.GetLeftPart([UriPartial]::Authority)
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run project registration in an elevated PowerShell window.'
}
$configPath = 'C:\ProgramData\TaskFolderMcp\service.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.tasksRoot -ne 'D:\Projects\Tracker\tasks') { throw 'TaskFolderMcp is not configured for production tasks.' }
$worker = Join-Path $config.installRoot 'folder-worker.js'
function Invoke-Worker([string]$Method, [hashtable]$Arguments) {
    $request = @{ method = $Method; arguments = $Arguments } | ConvertTo-Json -Compress -Depth 6
    $response = $request | & $config.nodePath $worker $configPath --admin | ConvertFrom-Json
    if (-not $response.ok) { throw "$($response.code): $($response.message)" }
    return $response.data
}
$entry = @{ project_key = $ProjectKey; source_type = $SourceType }
if ($Name) { $entry.name = $Name }
if ($SourceType -eq 'NO_JIRA') { $entry.next_issue_number = $NextIssueNumber }
else { $entry.jira_host = $JiraHost }
$current = (Invoke-Worker 'get_task_projects' @{}).manifest.projects |
    Where-Object project_key -EQ $ProjectKey
if ($current) {
    $same = $current.source_type -eq $SourceType -and ([string]$current.name) -eq ([string]$Name) -and
        (($SourceType -eq 'NO_JIRA' -and $current.next_issue_number -ge $NextIssueNumber) -or
         ($SourceType -ne 'NO_JIRA' -and $current.jira_host -eq $JiraHost))
    if (-not $same) { throw "Project $ProjectKey already exists with different settings." }
    Write-Output "Already registered: $ProjectKey"
    return
}
$result = Invoke-Worker 'register_task_project' $entry
Write-Output "Registered: $($result.project.project_key)"
