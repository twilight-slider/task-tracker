$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path $repo ('.runtime\tests\test-servicehost-acl\run-' + $PID)
$protected = Join-Path $testRoot '.protected'
New-Item -ItemType Directory -Path $protected -Force | Out-Null
$exe = Join-Path $testRoot 'TaskTrackerService.exe'
$csc = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs')
if ($LASTEXITCODE -ne 0) { throw 'Service host did not compile.' }
& icacls.exe $protected /grant '*S-1-5-11:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create the test ACL drift.' }
$configPath = [string](Join-Path $protected 'service.json')
$probeSource = Join-Path $testRoot 'AclProbe.cs'
$probeExe = Join-Path $testRoot 'AclProbe.exe'
@'
using System;
using System.Reflection;

internal static class AclProbe
{
    private static int Main(string[] args)
    {
        var assembly = Assembly.LoadFrom(args[0]);
        var repair = assembly.GetType("ProtectedRootAcl", true).GetMethod("Repair", BindingFlags.Static | BindingFlags.NonPublic);
        repair.Invoke(null, new object[] { args[1] });
        repair.Invoke(null, new object[] { args[1] });
        return 0;
    }
}
'@ | Set-Content -LiteralPath $probeSource -Encoding utf8
& $csc /nologo /target:exe "/out:$probeExe" $probeSource
if ($LASTEXITCODE -ne 0) { throw 'ACL probe did not compile.' }
& $probeExe $exe $configPath
if ($LASTEXITCODE -ne 0) { throw 'Service host ACL repair failed.' }
$acl = Get-Acl -LiteralPath $protected
$serviceSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @('S-1-5-18', 'S-1-5-32-544', $serviceSid)
$actual = @($acl.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
if (-not $acl.AreAccessRulesProtected -or
    $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $serviceSid -or
    @($actual | Where-Object { $_ -notin $allowed }).Count -or
    @($actual | Select-Object -Unique).Count -ne 3) { throw 'Protected root ACL was not repaired.' }
Write-Output 'Service host protected-root ACL repair passed'
