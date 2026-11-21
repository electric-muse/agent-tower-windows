param([switch]$Start)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    uv venv (Join-Path $root '.venv') --python 3.14
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment' }
}
uv pip install -p $python -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install Python dependencies' }
$venvConfig = Get-Content (Join-Path $root '.venv\pyvenv.cfg') -Encoding utf8
$baseHome = ($venvConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1) -replace '^home\s*=\s*', ''
$servicePython = Join-Path $baseHome 'python.exe'
if (-not (Test-Path $servicePython)) { throw 'Base Python executable not found' }
$sitePackages = Join-Path $root '.venv\Lib\site-packages'
$runtime = Join-Path $root '.dsh-runtime'
$dshEntry = Join-Path $runtime 'node_modules\@deepseek-ai\dsh\lib\bin.js'
$dshVersion = if (Test-Path $dshEntry) { & node.exe $dshEntry --version } else { '' }
if ($dshVersion -ne '0.1.7-rc.2') {
    & npm.cmd install --prefix $runtime --no-save --no-package-lock '@deepseek-ai/dsh@0.1.7-rc.2'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install the supported dsh version' }
}
if ((& node.exe $dshEntry --version) -ne '0.1.7-rc.2') { throw 'dsh version check failed' }

foreach ($name in @('ctl.conf', 'stream.conf')) {
    $target = Join-Path $root "config\$name"
    if (-not (Test-Path $target)) {
        Copy-Item (Join-Path $root "config\$name.example") $target
    }
}
$app = Join-Path $root 'config\feishu-app.json'
if (-not (Test-Path $app)) {
    Copy-Item (Join-Path $root 'config\feishu-app.example.json') $app
    $template = Get-Content $app -Raw -Encoding utf8 | ConvertFrom-Json
    $template.chat_id = ''
    [System.IO.File]::WriteAllText($app, ($template | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
}
New-Item -ItemType Directory -Force (Join-Path $root 'state'), (Join-Path $root 'logs') | Out-Null

$config = Join-Path $root 'config\ctl.conf'
$lines = [System.Collections.Generic.List[string]]::new()
$lines.AddRange([string[]](Get-Content $config -Encoding utf8))
$codex = & $python -c 'import sys; sys.path.insert(0, sys.argv[1]); from tower_platform import codex_executable; print(codex_executable())' (Join-Path $root 'scripts')
if (-not $codex) { throw 'Codex native executable not found. Install @openai/codex first.' }
$roots = (Split-Path $root -Parent).Replace('\', '/')
foreach ($entry in @{
    work_roots = $roots
    codex_bin = $codex.Replace('\', '/')
    dsh_bin = $dshEntry.Replace('\', '/')
    default_perm = 'safe'
    auto_watch = '0'
}.GetEnumerator()) {
    if (-not ($lines | Where-Object { $_ -match "^$($entry.Key)=" })) {
        $lines.Add("$($entry.Key)=$($entry.Value)")
    } elseif ($entry.Key -eq 'work_roots' -and ($lines | Where-Object { $_ -eq 'work_roots=/tmp' })) {
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -eq 'work_roots=/tmp') { $lines[$i] = "work_roots=$($entry.Value)" }
        }
    } elseif ($entry.Key -eq 'default_perm') {
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -eq 'default_perm=full') { $lines[$i] = 'default_perm=safe' }
        }
    } elseif ($entry.Key -eq 'auto_watch') {
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -eq 'auto_watch=1') { $lines[$i] = 'auto_watch=0' }
        }
    }
}
[System.IO.File]::WriteAllLines($config, $lines, [System.Text.UTF8Encoding]::new($false))

$settings = Get-Content $app -Raw -Encoding utf8 | ConvertFrom-Json
if ($settings.app_id -eq 'cli_xxxxxxxxxxxxx' -or $settings.app_secret -like 'REPLAC*') {
    Write-Host "Configure app_id and app_secret in $app"
    Write-Host 'Then send #help in Feishu, read open_id and chat_id from the bot reply, and set them in local config.'
    if ($Start) { throw 'Feishu app credentials are missing' }
    exit 0
}
$previousPythonPath = $env:PYTHONPATH
try {
    # A venv executable inside a low-integrity checkout launches low-integrity children on Windows.
    $env:PYTHONPATH = $sitePackages + $(if ($previousPythonPath) { ";$previousPythonPath" } else { '' })
    & $servicePython (Join-Path $root 'scripts\tower.py') --selftest
    if ($LASTEXITCODE -ne 0) { throw 'Self-test failed' }
    if ($Start) { & $servicePython (Join-Path $root 'scripts\tower.py') }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
