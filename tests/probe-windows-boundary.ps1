param(
    [Parameter(Mandatory)]
    [ValidateSet('Server', 'Agent')]
    [string]$Mode,
    [Parameter(Mandatory)]
    [string]$ProtectedRoot,
    [string]$AgentAccount = 'VASIL\Vasil-AI-Agent',
    [string]$PipeName = 'aidev58-boundary-probe',
    [switch]$SkipPipe
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Pipes.AccessControl

$serviceIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$serviceSid = $serviceIdentity.User
$agentSid = (New-Object Security.Principal.NTAccount($AgentAccount)).Translate([Security.Principal.SecurityIdentifier])
$root = [IO.Path]::GetFullPath($ProtectedRoot)
$parent = [IO.Path]::GetDirectoryName($root)
$sentinel = [IO.Path]::Combine($root, 'sentinel.txt')

if ($Mode -eq 'Server') {
    if ($serviceSid -eq $agentSid) {
        throw "Server runs as $($serviceIdentity.Name), the same account as $AgentAccount. Open PowerShell under a separate MCP service account; UAC elevation does not change the account SID."
    }
    if ([IO.Directory]::Exists($parent) -or [IO.File]::Exists($parent)) { throw 'Use a new, empty parent for ProtectedRoot.' }

    [IO.Directory]::CreateDirectory($root) | Out-Null
    foreach ($directory in @($parent, $root)) {
        $acl = Get-Acl -LiteralPath $directory
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($identity in @($serviceSid, 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
            $acl.AddAccessRule($rule)
        }
        $readRule = New-Object Security.AccessControl.FileSystemAccessRule($agentSid, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($readRule)
        Set-Acl -LiteralPath $directory -AclObject $acl
    }
    [IO.File]::WriteAllText($sentinel, 'protected')

    $pipeAcl = New-Object System.IO.Pipes.PipeSecurity
    $pipeAcl.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule($serviceSid, 'FullControl', 'Allow')))
    $pipeAcl.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule($agentSid, 'ReadWrite', 'Allow')))
    $pipe = [System.IO.Pipes.NamedPipeServerStreamAcl]::Create($PipeName, 'InOut', 1, 'Byte', 'Asynchronous', 4096, 4096, $pipeAcl)
    try {
        Write-Output "READY pipe=$PipeName root=$root service=$serviceSid agent=$agentSid"
        $connection = $pipe.WaitForConnectionAsync()
        $deadline = [DateTime]::UtcNow.AddMinutes(10)
        while (-not $connection.Wait(200)) {
            if ([DateTime]::UtcNow -ge $deadline) { throw 'Timed out waiting for the agent.' }
        }
        $null = $connection.GetAwaiter().GetResult()
        $reader = New-Object IO.StreamReader($pipe)
        $writer = New-Object IO.StreamWriter($pipe)
        $writer.AutoFlush = $true
        if ($reader.ReadLine() -ne 'PING') { throw 'Unexpected pipe request.' }
        $writer.WriteLine('PONG')
        if ($reader.ReadLine() -ne 'DONE') { throw 'Agent probe did not finish.' }
        if (-not [IO.File]::Exists($sentinel) -or [IO.File]::ReadAllText($sentinel) -ne 'protected') {
            throw 'Protected sentinel was changed.'
        }
    } finally { $pipe.Dispose() }
    return
}

if ($serviceSid -ne $agentSid) { throw "Start Agent as $AgentAccount." }
$client = $null
try {
    if (-not $SkipPipe) {
        $client = New-Object System.IO.Pipes.NamedPipeClientStream('.', $PipeName, 'InOut')
        $client.Connect(5000)
        $reader = New-Object IO.StreamReader($client)
        $writer = New-Object IO.StreamWriter($client)
        $writer.AutoFlush = $true
        $writer.WriteLine('PING')
        if ($reader.ReadLine() -ne 'PONG') { throw 'Local service call failed.' }
    }

    $failed = @()
    function Expect-Denied([string]$name, [scriptblock]$operation) {
        try {
            & $operation
            $script:failed += $name
        } catch [UnauthorizedAccessException] {
        } catch [Security.SecurityException] {
        } catch [IO.IOException] {
            if ($_.Exception.HResult -ne -2147024891) { throw }
        }
    }

    Expect-Denied 'write' { [IO.File]::WriteAllText([IO.Path]::Combine($root, 'new.txt'), 'changed') }
    Expect-Denied 'overwrite' { [IO.File]::WriteAllText($sentinel, 'changed') }
    Expect-Denied 'delete' { [IO.File]::Delete($sentinel) }
    Expect-Denied 'delete-root' { [IO.Directory]::Delete($root, $true) }
    Expect-Denied 'rename-file' { [IO.File]::Move($sentinel, [IO.Path]::Combine($root, 'renamed.txt')) }
    Expect-Denied 'rename-root' { [IO.Directory]::Move($root, "${root}-renamed") }
    Expect-Denied 'rename-parent' { [IO.Directory]::Move($parent, "${parent}-renamed") }
    Expect-Denied 'change-acl' {
        $acl = Get-Acl -LiteralPath $root
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($agentSid, 'FullControl', 'Allow')))
        Set-Acl -LiteralPath $root -AclObject $acl
    }
    if ($writer) { $writer.WriteLine('DONE') }
    if ($failed.Count) { throw "Protection failed for: $($failed -join ', ')" }
    if ($SkipPipe) {
        Write-Output 'PASS: protected writes, delete, rename and ACL change were denied (pipe not tested).'
    } else {
        Write-Output 'PASS: pipe call succeeded; protected writes, delete, rename and ACL change were denied.'
    }
} finally { if ($client) { $client.Dispose() } }
