param([string]$WorkspacePath)

$ErrorActionPreference = 'Stop'
$envFile = Join-Path $env:USERPROFILE '.env\env.txt'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw "TargetUser env.txt is missing: $envFile" }
$lines = @([IO.File]::ReadAllLines($envFile, [Text.UTF8Encoding]::new($false, $true)) |
    Where-Object { $_ -match '^\s*TRACKER_MCP_TOKEN\s*=' })
if ($lines.Count -ne 1) { throw 'env.txt must contain exactly one TRACKER_MCP_TOKEN.' }
$token = ($lines[0] -split '=', 2)[1].Trim()
if (-not $token) { throw 'TRACKER_MCP_TOKEN is empty.' }
$running = @(Get-CimInstance Win32_Process -Filter "Name='codex.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like '*\OpenAI\Codex\bin\*' })
if ($running.Count) { throw 'Close Codex Desktop completely, then rerun this launcher so the new process receives TRACKER_MCP_TOKEN.' }
$codex = (Get-Command codex -ErrorAction Stop).Source
$env:TRACKER_MCP_TOKEN = $token
if ($WorkspacePath) {
    if (-not (Test-Path -LiteralPath $WorkspacePath -PathType Container)) { throw "Workspace does not exist: $WorkspacePath" }
    & $codex app $WorkspacePath
} else {
    & $codex app
}
if ($LASTEXITCODE -ne 0) { throw 'Codex Desktop did not start.' }
