param(
    [Parameter(Mandatory)][string]$RuntimeRoot,
    [string]$PythonExe,
    [string]$RequirementsPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'requirements.txt')
)

$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathFullyQualified($RuntimeRoot) -or -not [IO.Path]::IsPathFullyQualified($RequirementsPath)) {
    throw 'RuntimeRoot and RequirementsPath must be absolute paths.'
}
$runtime = [IO.Path]::GetFullPath($RuntimeRoot)
$requirements = [IO.Path]::GetFullPath($RequirementsPath)
$requirementsFile = Get-Item -LiteralPath $requirements -Force -ErrorAction Stop
if ($requirementsFile.PSIsContainer -or ($requirementsFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Requirements must be a plain file: $requirements"
}
if (-not $PythonExe) {
    $PythonExe = Get-Command python.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1 -ExpandProperty Source
}
if (-not [IO.Path]::IsPathFullyQualified($PythonExe)) { throw 'PythonExe must be an absolute path.' }
$sourcePython = [IO.Path]::GetFullPath($PythonExe)
$pythonFile = Get-Item -LiteralPath $sourcePython -Force -ErrorAction Stop
if ($pythonFile.PSIsContainer -or ($pythonFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Installed Python must be a plain executable: $sourcePython"
}
$sourceVersion = & $sourcePython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))' 2>&1
if ($LASTEXITCODE -ne 0 -or $sourceVersion -ne '3.11') {
    throw "Python 3.11 is required in PATH; found '$sourceVersion' at $sourcePython. Install Python 3.11 or update PATH."
}
if (Test-Path -LiteralPath $runtime) {
    $existing = Get-Item -LiteralPath $runtime -Force
    if (-not $existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $runtime -Force).Count) {
        throw "RuntimeRoot must be an empty plain directory: $runtime"
    }
}

& $sourcePython -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 could not create the protected virtual environment: $runtime" }
$python = Join-Path $runtime 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Virtual environment Python is missing: $python" }
& $python -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw "pip could not install dependencies from $requirements" }
$probe = & $python -I -c 'import sys,yaml; print(sys.version_info.major, sys.version_info.minor, sep=chr(46), end=chr(32)); print(yaml.__version__, sys.flags.isolated)' 2>&1
if ($LASTEXITCODE -ne 0 -or $probe -notmatch '^3\.11 \S+ 1$') { throw "Virtual environment Python/PyYAML import failed: $probe" }
[pscustomobject]@{
    RuntimeRoot = $runtime
    PythonPath = $python
    PythonVersion = '3.11'
    PyYAMLVersion = ($probe -split ' ')[1]
}
