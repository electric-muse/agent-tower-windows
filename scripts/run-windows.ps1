param([switch]$SelfTest)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvConfig = Join-Path $root '.venv\pyvenv.cfg'
if (-not (Test-Path $venvConfig)) { throw 'Agent Tower virtual environment is missing' }
$homeLine = Get-Content $venvConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
if (-not $homeLine) { throw 'Base Python home is missing from pyvenv.cfg' }
$baseHome = ($homeLine -replace '^home\s*=\s*', '').Trim()
$python = Join-Path $baseHome 'python.exe'
$sitePackages = Join-Path $root '.venv\Lib\site-packages'
if (-not (Test-Path $python) -or -not (Test-Path $sitePackages)) {
    throw 'Agent Tower Python runtime is incomplete'
}

$env:PYTHONPATH = $sitePackages + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { '' })
$entry = Join-Path $root 'scripts\tower.py'
if ($SelfTest) { & $python $entry --selftest }
else { & $python $entry }
exit $LASTEXITCODE
