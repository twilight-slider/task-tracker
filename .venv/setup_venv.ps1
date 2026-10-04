$ErrorActionPreference = 'Stop'
$venvRoot = $PSScriptRoot
$repoRoot = [IO.Path]::GetFullPath((Join-Path $venvRoot '..'))
$versionFile = Join-Path $venvRoot 'python_version.txt'
$requirementsFile = Join-Path $repoRoot 'requirements.txt'
$devRequirementsFile = Join-Path $repoRoot 'requirements-dev.txt'

if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw "Не найден файл версии Python: $versionFile"
}
$requestedVersion = [IO.File]::ReadAllText($versionFile).Trim()
if ($requestedVersion -notmatch '^python3\.(\d+)$') {
    throw "Некорректная версия Python '$requestedVersion' в $versionFile. Укажите python3.x; рекомендуется python3.11."
}
$minorVersion = [int]$Matches[1]
$selector = "-3.$minorVersion"
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $launcher) {
    throw "Не найден py.exe. Установите $requestedVersion или измените $versionFile на установленную версию; рекомендуется python3.11."
}

try {
    $detectedVersion = (& $launcher.Source $selector -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))' 2>$null | Select-Object -Last 1).Trim()
} catch {
    $detectedVersion = $null
}
if ($LASTEXITCODE -ne 0 -or $detectedVersion -ne "3.$minorVersion") {
    throw "Версия $requestedVersion недоступна. Установите её или измените $versionFile на установленную версию; рекомендуется python3.11."
}

& $launcher.Source $selector -m venv $venvRoot
if ($LASTEXITCODE -ne 0) { throw "Не удалось создать виртуальное окружение с $requestedVersion." }

$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Не найден Python в виртуальном окружении: $venvPython"
}
$actualVersion = (& $venvPython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))' | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or $actualVersion -ne "3.$minorVersion") {
    throw "В виртуальном окружении версия '$actualVersion', ожидалась 3.$minorVersion."
}

foreach ($file in @($requirementsFile, $devRequirementsFile)) {
    if (Test-Path -LiteralPath $file -PathType Leaf) {
        & $venvPython -m pip install --disable-pip-version-check -r $file
        if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости из $file" }
    }
}

Write-Output "Окружение готово: $venvPython ($requestedVersion)"
