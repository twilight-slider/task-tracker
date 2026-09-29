param(
    [ValidateSet('Test', 'Production')][string]$Target = 'Production',
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
if (-not $ConfigPath) {
    $name = if ($Target -eq 'Production') { 'task-folder-mcp.json' } else { 'task-folder-mcp.test.json' }
    $ConfigPath = Join-Path $HOME ".env\$name"
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run bootstrap without elevation.'
}
if (-not [IO.Path]::IsPathRooted($ConfigPath)) { throw 'ConfigPath must be absolute.' }
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)

$serviceAccount = Get-LocalUser -Name 'TaskFolderMcpSvc' -ErrorAction SilentlyContinue
$config = [ordered]@{
    schemaVersion = 1
    ownerSid = $identity.User.Value
    agentSid = $identity.User.Value
    serviceName = 'TaskFolderMcp'
    serviceAccountName = 'TaskFolderMcpSvc'
    serviceAccountSid = if ($serviceAccount) { $serviceAccount.SID.Value } else { $null }
    pipeName = 'task-folder-mcp-v1'
    nodePath = (Get-Command node.exe -ErrorAction Stop).Source
    tasksRoot = if ($Target -eq 'Production') { 'D:\Projects\Tracker\tasks' } else { 'D:\Projects\Tracker\task-folder-mcp-tests\tasks' }
    protectedRoot = if ($Target -eq 'Production') { 'C:\ProgramData\TaskFolderMcp\protected' } else { 'C:\ProgramData\TaskFolderMcp\protected-test' }
    installRoot = 'C:\Program Files\TaskFolderMcp'
}
$json = $config | ConvertTo-Json -Depth 3
$directory = [IO.Path]::GetDirectoryName($ConfigPath)
if (Test-Path -LiteralPath $directory) {
    $item = Get-Item -LiteralPath $directory -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Config directory must be a regular directory: $directory"
    }
} else {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
if (Test-Path -LiteralPath $ConfigPath) {
    $item = Get-Item -LiteralPath $ConfigPath -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Config path must be a regular file: $ConfigPath"
    }
    $existing = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if ($existing.serviceAccountSid -and
        (-not $serviceAccount -or $existing.serviceAccountSid -ne $serviceAccount.SID.Value)) {
        throw "Service account SID changed after bootstrap: $ConfigPath"
    }
    # The installer may create the service account after the first bootstrap run.
    $config.serviceAccountSid = $existing.serviceAccountSid
    $json = $config | ConvertTo-Json -Depth 3
    if (($existing | ConvertTo-Json -Depth 3) -ne $json) {
        $legacyTest = $json | ConvertFrom-Json
        $legacyTest.tasksRoot = 'D:\Projects\Tracker\task-folder-mcp-tests\tasks'
        $legacyTest.protectedRoot = 'C:\ProgramData\TaskFolderMcp\protected-test'
        if ($Target -ne 'Production' -or ($existing | ConvertTo-Json -Depth 3) -ne
            ($legacyTest | ConvertTo-Json -Depth 3)) {
            throw "Existing config differs; review it before installation: $ConfigPath"
        }
        $testPath = Join-Path $directory "$([IO.Path]::GetFileNameWithoutExtension($ConfigPath)).test.json"
        if (Test-Path -LiteralPath $testPath) {
            $savedTest = Get-Content -LiteralPath $testPath -Raw | ConvertFrom-Json
            if (($savedTest | ConvertTo-Json -Depth 3) -ne ($existing | ConvertTo-Json -Depth 3)) {
                throw "Existing test config differs; review it before migration: $testPath"
            }
        } else {
            Copy-Item -LiteralPath $ConfigPath -Destination $testPath
        }
        [IO.File]::WriteAllText($ConfigPath, "$json`n", [Text.UTF8Encoding]::new($false))
        Write-Output "Updated to production: $ConfigPath; previous test config: $testPath"
        return
    }
    Write-Output "Already prepared: $ConfigPath"
    return
}
[IO.File]::WriteAllText($ConfigPath, "$json`n", [Text.UTF8Encoding]::new($false))
Write-Output "Prepared: $ConfigPath"
