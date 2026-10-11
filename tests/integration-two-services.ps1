param(
    [Parameter(Mandatory)][string]$OtherServiceName,
    [Parameter(Mandatory)][ValidateRange(1, 65535)][int]$OtherPort
)

$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$ownServiceName = 'TaskTracker-' + $sid.Split('-')[-1]
if ($OtherServiceName -notmatch '^TaskTracker-\d+$' -or $OtherServiceName -eq $ownServiceName) {
    throw 'OtherServiceName must identify a different TaskTracker service.'
}

$envPath = Join-Path $env:USERPROFILE '.env\env.txt'
$lines = [IO.File]::ReadAllLines($envPath)
function Get-Setting([string]$name) {
    $values = @($lines | Where-Object { $_.StartsWith("$name=", [StringComparison]::Ordinal) })
    if ($values.Count -ne 1 -or $values[0].Length -eq $name.Length + 1) {
        throw "Expected exactly one nonempty $name in the current user's env file."
    }
    return $values[0].Substring($name.Length + 1)
}

$ownPortText = Get-Setting 'TRACKER_MCP_PORT'
if ($ownPortText -notmatch '^[0-9]+$') { throw 'TRACKER_MCP_PORT is invalid.' }
$ownPort = 0
if (-not [int]::TryParse($ownPortText, [ref]$ownPort) -or $ownPort -lt 1 -or $ownPort -gt 65535 -or $ownPort -eq $OtherPort) {
    throw 'The two services must have distinct valid MCP ports.'
}
$token = Get-Setting 'TRACKER_MCP_TOKEN'
$tasksRoot = Join-Path (Get-Setting 'TRACKER_FOLDER') 'tasks'

foreach ($name in @($ownServiceName, $OtherServiceName)) {
    $service = Get-Service -Name $name -ErrorAction Stop
    if ($service.Status -ne 'Running' -or $service.StartType -ne 'Automatic') {
        throw "$name must be Running and Automatic."
    }
}

$body = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_tasks_folder","arguments":{}}}'
function Invoke-Tracker([int]$port) {
    Invoke-WebRequest -Uri "http://127.0.0.1:$port/mcp/folders" -Method Post `
        -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' `
        -Body $body -SkipHttpErrorCheck
}
$own = Invoke-Tracker $ownPort
$other = Invoke-Tracker $OtherPort
if ([int]$own.StatusCode -ne 200 -or -not $own.Content.Replace('\\', '\').Contains($tasksRoot)) {
    throw 'The current user did not reach their own tasks root.'
}
if ([int]$other.StatusCode -ne 401) { throw 'The current user reached the other service.' }

function Test-Pipe([string]$name) {
    $pipe = [IO.Pipes.NamedPipeClientStream]::new('.', $name, [IO.Pipes.PipeDirection]::InOut)
    try {
        $pipe.Connect(2000)
        return $true
    } catch [UnauthorizedAccessException] {
        return $false
    } finally {
        $pipe.Dispose()
    }
}
$ownPipe = 'task-tracker-' + $sid.Split('-')[-1] + '-v1'
$otherPipe = 'task-tracker-' + $OtherServiceName.Substring('TaskTracker-'.Length) + '-v1'
$ownPipeConnected = Test-Pipe $ownPipe
$otherPipeConnected = Test-Pipe $otherPipe
if (-not $ownPipeConnected -or $otherPipeConnected) {
    throw "Named pipe access is not isolated: $ownPipe=$ownPipeConnected, $otherPipe=$otherPipeConnected."
}

Write-Output "${ownServiceName}: own MCP 200, other MCP 401, own pipe connected, other pipe denied; both services Running/Automatic."
