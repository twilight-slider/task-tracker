param(
    [Parameter(Mandatory)][string]$RuntimeRoot,
    [string]$PythonExe,
    [string]$VersionFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\python_version.txt'),
    [string]$RequirementsPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'requirements.txt')
)

$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathFullyQualified($RuntimeRoot) -or
    -not [IO.Path]::IsPathFullyQualified($VersionFile) -or
    -not [IO.Path]::IsPathFullyQualified($RequirementsPath)) {
    throw 'RuntimeRoot, VersionFile and RequirementsPath must be absolute paths.'
}
$runtime = [IO.Path]::GetFullPath($RuntimeRoot)
$versionPath = [IO.Path]::GetFullPath($VersionFile)
$versionItem = Get-Item -LiteralPath $versionPath -Force -ErrorAction Stop
if ($versionItem.PSIsContainer -or ($versionItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Python version setting must be a plain file: $versionPath"
}
$versionSetting = [IO.File]::ReadAllText($versionPath).Trim()
if ($versionSetting -notmatch '^python3\.(\d+)$') { throw "Invalid Python version setting in $versionPath." }
$wantedVersion = "3.$($Matches[1])"
$requirements = [IO.Path]::GetFullPath($RequirementsPath)
$requirementsFile = Get-Item -LiteralPath $requirements -Force -ErrorAction Stop
if ($requirementsFile.PSIsContainer -or ($requirementsFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Requirements must be a plain file: $requirements"
}
if (-not $PythonExe) {
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1 -ExpandProperty Source
    $launcherFile = Get-Item -LiteralPath $launcher -Force -ErrorAction Stop
    if ($launcherFile.PSIsContainer -or ($launcherFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Python launcher must be a plain executable: $launcher"
    }
    $installed = & $launcher -0p 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Python launcher could not list installed versions.' }
    $installedText = $installed -join "`n"
    $versionPattern = '(?m)^\s*-V:(?:[^/\s]+/)?' + [Regex]::Escape($wantedVersion) + '(?:\s|$)'
    if ($installedText -notmatch $versionPattern) {
        throw "Python $wantedVersion is unavailable. Install it or change $versionPath; Python 3.11 is recommended."
    }
    $selector = "-$wantedVersion"
    $allowInstall = $env:PYLAUNCHER_ALLOW_INSTALL
    try {
        $env:PYLAUNCHER_ALLOW_INSTALL = $null
        $selection = & $launcher $selector -c 'import sys; print(sys.executable)' 2>&1
        $selectionExitCode = $LASTEXITCODE
    } finally {
        $env:PYLAUNCHER_ALLOW_INSTALL = $allowInstall
    }
    if ($selectionExitCode -ne 0 -or @($selection).Count -ne 1 -or
        -not [IO.Path]::IsPathFullyQualified([string]$selection)) {
        throw "Python $wantedVersion could not be selected through py.exe; no Python version was downloaded or installed."
    }
    $PythonExe = [string]$selection
}
if (-not [IO.Path]::IsPathFullyQualified($PythonExe)) { throw 'PythonExe must be an absolute path.' }
$sourcePython = [IO.Path]::GetFullPath($PythonExe)
$pythonFile = Get-Item -LiteralPath $sourcePython -Force -ErrorAction Stop
if ($pythonFile.PSIsContainer -or ($pythonFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Installed Python must be a plain executable: $sourcePython"
}
$sourceVersion = & $sourcePython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))' 2>&1
if ($LASTEXITCODE -ne 0 -or $sourceVersion -ne $wantedVersion) {
    throw "Python $wantedVersion is required; found '$sourceVersion' at $sourcePython. Install it or change $versionPath; Python 3.11 is recommended."
}
if (Test-Path -LiteralPath $runtime) {
    $existing = Get-Item -LiteralPath $runtime -Force
    if (-not $existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $runtime -Force).Count) {
        throw "RuntimeRoot must be an empty plain directory: $runtime"
    }
}

& $sourcePython -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw "Python $wantedVersion could not create the protected virtual environment: $runtime" }
$python = Join-Path $runtime 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Virtual environment Python is missing: $python" }
& $python -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw "pip could not install dependencies from $requirements" }
$probe = & $python -I -c 'import sys,yaml; print(sys.version_info.major, sys.version_info.minor, sep=chr(46), end=chr(32)); print(yaml.__version__, sys.flags.isolated)' 2>&1
if ($LASTEXITCODE -ne 0 -or $probe -notmatch ('^' + [Regex]::Escape($wantedVersion) + ' \S+ 1$')) {
    throw "Virtual environment Python/PyYAML import failed: $probe"
}
[pscustomobject]@{
    RuntimeRoot = $runtime
    PythonPath = $python
    PythonVersion = $wantedVersion
    PyYAMLVersion = ($probe -split ' ')[1]
}
