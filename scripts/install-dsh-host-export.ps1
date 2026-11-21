[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProfileDirectory = (Join-Path $env:USERPROFILE '.dsh\profiles\desktop')
)

$ErrorActionPreference = 'Stop'
$towerRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pluginFile = Join-Path $PSScriptRoot 'dsh-host-export.mjs'
$outputFile = Join-Path $towerRoot 'config\dsh-host.json'
$profilePatch = Join-Path $ProfileDirectory 'cordis.patch.yml'
if (-not (Test-Path -LiteralPath $pluginFile) -or -not (Test-Path -LiteralPath $profilePatch)) {
    throw 'Tower plugin or DSH desktop profile patch is missing'
}
$original = [IO.File]::ReadAllText($profilePatch, [Text.Encoding]::UTF8)
if ($original -match '(?m)^\s*- id: agent-tower-host-export\s*$') {
    Write-Output 'Agent Tower DSH Host export is already registered'
    return
}
$pluginUrl = ([Uri]::new($pluginFile)).AbsoluteUri | ConvertTo-Json -Compress
$quotedOutput = $outputFile.Replace('\', '/') | ConvertTo-Json -Compress
$addition = "`n# Agent Tower: publish the current loopback Host address to local private config.`n- insert:`n    - id: agent-tower-host-export`n      name: $pluginUrl`n      config:`n        outputFile: $quotedOutput`n"
if ($PSCmdlet.ShouldProcess($profilePatch, 'Back up and append the Agent Tower Host export plugin')) {
    $backup = $profilePatch + '.tower-backup-' + [guid]::NewGuid().ToString('N')
    Copy-Item -LiteralPath $profilePatch -Destination $backup
    [IO.File]::WriteAllText($profilePatch, $original + $addition, [Text.UTF8Encoding]::new($false))
    Write-Output 'Registered Agent Tower Host export; DSH can reload its profile automatically'
}
