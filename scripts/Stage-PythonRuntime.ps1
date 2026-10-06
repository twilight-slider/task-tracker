param(
    [Parameter(Mandatory)][string]$RuntimeRoot,
    [string]$PackageRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'vendor')
)

$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathFullyQualified($RuntimeRoot) -or -not [IO.Path]::IsPathFullyQualified($PackageRoot)) {
    throw 'RuntimeRoot and PackageRoot must be absolute paths.'
}
$runtime = [IO.Path]::GetFullPath($RuntimeRoot)
$packages = [IO.Path]::GetFullPath($PackageRoot)
$pythonZip = Join-Path $packages 'python-3.11.9-embed-amd64.zip'
$yamlWheel = Join-Path $packages 'pyyaml-6.0.3-cp311-cp311-win_amd64.whl'
$expected = @(
    @{ Path = $pythonZip; Hash = '009D6BF7E3B2DDCA3D784FA09F90FE54336D5B60F0E0F305C37F400BF83CFD3B' },
    @{ Path = $yamlWheel; Hash = '9F3BFB4965EB874431221A3FF3FDCDDC7E74E3B07799E0E84CA4A0F867D449BF' }
)
foreach ($item in $expected) {
    $file = Get-Item -LiteralPath $item.Path -Force -ErrorAction Stop
    if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Package is not a plain file: $($item.Path)"
    }
    $actual = (Get-FileHash -LiteralPath $item.Path -Algorithm SHA256).Hash
    if ($actual -cne $item.Hash) { throw "Package hash mismatch: $($item.Path)" }
}
if (Test-Path -LiteralPath $runtime) {
    $existing = Get-Item -LiteralPath $runtime -Force
    if (-not $existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $runtime -Force).Count) {
        throw "RuntimeRoot must be an empty plain directory: $runtime"
    }
} else {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
}

$privatePackages = Join-Path $runtime '.packages'
New-Item -ItemType Directory -Path $privatePackages | Out-Null
$pythonCopy = Join-Path $privatePackages (Split-Path -Leaf $pythonZip)
$yamlCopy = Join-Path $privatePackages (Split-Path -Leaf $yamlWheel)
Copy-Item -LiteralPath $pythonZip -Destination $pythonCopy
Copy-Item -LiteralPath $yamlWheel -Destination $yamlCopy
if ((Get-FileHash -LiteralPath $pythonCopy -Algorithm SHA256).Hash -cne $expected[0].Hash -or
    (Get-FileHash -LiteralPath $yamlCopy -Algorithm SHA256).Hash -cne $expected[1].Hash) {
    throw 'Staged package hash mismatch.'
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($pythonCopy, $runtime)
$pth = Join-Path $runtime 'python311._pth'
if (-not (Test-Path -LiteralPath $pth -PathType Leaf)) { throw 'Embedded Python path file is missing.' }
$sitePackages = Join-Path $runtime 'Lib\site-packages'
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
[IO.Compression.ZipFile]::ExtractToDirectory($yamlCopy, $sitePackages)
[IO.File]::WriteAllText($pth, "python311.zip`n.`nLib\site-packages`n", [Text.UTF8Encoding]::new($false))

$python = Join-Path $runtime 'python.exe'
$probe = 'import sys,yaml; print(sys.version_info.major,sys.version_info.minor,sys.version_info.micro,yaml.__version__,sys.flags.isolated)'
$result = & $python -I -c $probe 2>&1
if ($LASTEXITCODE -ne 0 -or $result -ne '3 11 9 6.0.3 1') { throw "Embedded Python/PyYAML import failed: $result" }
Remove-Item -LiteralPath $pythonCopy, $yamlCopy
Remove-Item -LiteralPath $privatePackages
[pscustomobject]@{
    RuntimeRoot = $runtime
    PythonVersion = '3.11.9'
    PyYAMLVersion = '6.0.3'
    PythonExeHash = (Get-FileHash -LiteralPath $python -Algorithm SHA256).Hash
    PackageHashes = @($expected | ForEach-Object { $_.Hash })
}
