#Requires -Version 7.0
Set-StrictMode -Version Latest

function Get-ArsDigest([byte[]]$Bytes) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Assert-ArsCredentialRoot([string]$Root) {
    if ($Root -notmatch '^[A-Za-z]:[\\/]') { throw 'LOCAL_CREDENTIAL_ROOT_REQUIRED' }
    $ancestor = $Root
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and
            ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'CREDENTIAL_LINK_REJECTED' }
        $ancestor = Split-Path -Parent $ancestor
    }
    if (Test-Path -LiteralPath $Root) {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $acl = Get-Acl -LiteralPath $Root
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid -or -not $acl.AreAccessRulesProtected) { throw 'UNTRUSTED_CREDENTIAL_ROOT' }
        foreach ($rule in $acl.Access) {
            if ($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin @($sid,'S-1-5-18') -or
                $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { throw 'UNTRUSTED_CREDENTIAL_ROOT' }
        }
    }
}

function New-ArsClientCredential {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]{1,64}$')][string]$ClientId,
        [string]$CredentialRoot = (Join-Path $env:LOCALAPPDATA 'AgentRuntimeClient')
    )
    if (-not $IsWindows) { throw 'WINDOWS_REQUIRED' }
    $root = [IO.Path]::GetFullPath($CredentialRoot)
    Assert-ArsCredentialRoot $root
    $path = Join-Path $root 'client-token.dpapi'
    $public = Join-Path $root 'client-verifier.json'
    if ((Test-Path -LiteralPath $path) -or (Test-Path -LiteralPath $public)) { throw 'CREDENTIAL_EXISTS: Существующая пара не заменяется.' }
    if (-not (Test-Path -LiteralPath $root)) {
        [IO.Directory]::CreateDirectory($root) | Out-Null
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        if ((Get-Acl -LiteralPath $root).GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'UNTRUSTED_CREDENTIAL_OWNER' }
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true,$false)
        foreach ($principal in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($principal,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
        }
        Set-Acl -LiteralPath $root -AclObject $acl
    }
    Assert-ArsCredentialRoot $root
    $bytes = [byte[]]::new(32)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $token = [BitConverter]::ToString($bytes).Replace('-','').ToLowerInvariant()
    $raw = $null
    try {
        $raw = [Text.Encoding]::ASCII.GetBytes($token)
        $protected = [Security.Cryptography.ProtectedData]::Protect($raw, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew)
        try { $stream.Write($protected,0,$protected.Length) } finally { $stream.Dispose() }
        $verifier = [ordered]@{ clients = @(@{ id = $ClientId; sha256 = Get-ArsDigest $raw; enabled = $true }) }
        $stream = [IO.File]::Open($public, [IO.FileMode]::CreateNew)
        try {
            $json = [Text.Encoding]::UTF8.GetBytes(($verifier | ConvertTo-Json -Depth 5))
            $stream.Write($json,0,$json.Length)
        } finally { $stream.Dispose() }
    } finally {
        [Array]::Clear($bytes,0,$bytes.Length)
        if ($null -ne $raw) { [Array]::Clear($raw,0,$raw.Length) }
        $token = $null
    }
    [pscustomobject]@{ CredentialPath = $path; PublicPolicyPath = $public; ClientId = $ClientId }
}

function Invoke-Ars {
    [CmdletBinding()]
    param(
        [ValidateSet('GET', 'POST')][string]$Method = 'GET',
        [Parameter(Mandatory)][ValidatePattern('^/v1/(console|health|readiness|images/deploy|(?:jobs|builds)(?:/[a-f0-9]{32}(?:/(?:result|stop|events|logs|commands|interrupt)(?:\?after_seq=\d{1,12})?)?)?)$')][string]$Path,
        [ValidateRange(1024,65535)][int]$Port = 48758,
        [string]$CredentialPath = (Join-Path $env:LOCALAPPDATA 'AgentRuntimeClient/client-token.dpapi'),
        [hashtable]$Payload = @{},
        [ValidatePattern('^[A-Za-z0-9_-]{1,128}$')][string]$IdempotencyKey = ([guid]::NewGuid().ToString('N'))
    )
    $raw = $null
    try {
        Assert-ArsCredentialRoot ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($CredentialPath)))
        if ((Get-Item -LiteralPath $CredentialPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'CREDENTIAL_LINK_REJECTED' }
        $raw = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($CredentialPath), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $parameters = @{ Uri = "http://127.0.0.1:$Port$Path"; Method = $Method; Headers = @{ Authorization = 'Bearer ' + [Text.Encoding]::ASCII.GetString($raw) }; TimeoutSec = $(if ($Path -eq '/v1/console') { 315 } else { 20 }); MaximumRedirection = 0; Verbose = $false; Debug = $false; ErrorAction = 'Stop' }
        if ($Method -eq 'POST') {
            $parameters.ContentType = 'application/json'
            $parameters.Body = @{ api_version = '1'; request_id = [guid]::NewGuid().ToString(); idempotency_key = $IdempotencyKey; payload = $Payload } | ConvertTo-Json -Depth 8 -Compress
        }
        Invoke-RestMethod @parameters
    } catch {
        $safe = 'ARS_REQUEST_FAILED'
        try {
            $failure = $_.ErrorDetails.Message | ConvertFrom-Json
            if ($failure.code -match '^[A-Z][A-Z0-9_]{0,63}$') { $safe += " code=$($failure.code)" }
            $correlation = [guid]::Empty
            if ([guid]::TryParse([string]$failure.request_id, [ref]$correlation)) { $safe += " request_id=$correlation" }
        } catch { }
        throw ($safe + ': Проверьте готовность ARS, credential и право клиента; секреты не включены в ошибку.')
    } finally {
        if ($null -ne $raw) { [Array]::Clear($raw,0,$raw.Length) }
        if (Get-Variable parameters -ErrorAction SilentlyContinue) { $parameters.Headers.Clear() }
    }
}
