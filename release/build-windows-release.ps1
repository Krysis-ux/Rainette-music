[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [switch]$RequireSigning
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $PSScriptRoot 'stage'
$output = Join-Path $PSScriptRoot 'out'
$appDir = Join-Path $stage 'RainetteMusic'
$installer = Join-Path $output 'RainetteMusicSetup.exe'
$webDir = Join-Path $root 'web'
$icon = Join-Path $webDir 'assets\rainette-icon.ico'
$entryPoint = Join-Path $root 'main.py'

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name is required but was not found on PATH." }
}
function Find-InnoCompiler {
    $command = Get-Command 'iscc' -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    throw 'Inno Setup 6 is required. Install it with: winget install --id JRSoftware.InnoSetup -e'
}
function Write-Checksum([string]$Path) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    Set-Content -LiteralPath "$Path.sha256" -Value "$hash  $(Split-Path -Leaf $Path)" -NoNewline
    return $hash
}

Require-Command 'python'
if ($RequireSigning) {
    foreach ($name in 'RAINETTE_CODESIGN_CERT_PATH', 'RAINETTE_CODESIGN_CERT_PASSWORD') {
        if (-not (Get-Item -Path "env:$name" -ErrorAction SilentlyContinue).Value) { throw "$name is required for a publish-ready Windows release." }
    }
    Require-Command 'signtool'
}
$iscc = Find-InnoCompiler

Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $output -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage, $output | Out-Null

Push-Location $root
try {
    python -m PyInstaller --noconfirm --clean --onedir --noconsole --name RainetteMusic `
        --distpath $stage --workpath (Join-Path $stage 'work') --specpath (Join-Path $stage 'spec') `
        --icon $icon `
        --add-data "$webDir;web" --collect-all webview --collect-all ytmusicapi --collect-all yt_dlp --collect-all qrcode $entryPoint
} finally { Pop-Location }

if (-not (Test-Path (Join-Path $appDir 'RainetteMusic.exe'))) { throw 'PyInstaller did not produce RainetteMusic.exe.' }
if ($RequireSigning) {
    & signtool sign /fd SHA256 /f $env:RAINETTE_CODESIGN_CERT_PATH /p $env:RAINETTE_CODESIGN_CERT_PASSWORD /tr http://timestamp.digicert.com /td SHA256 (Join-Path $appDir 'RainetteMusic.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Authenticode signing failed.' }
    & signtool verify /pa /v (Join-Path $appDir 'RainetteMusic.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Authenticode verification failed.' }
}

& $iscc "/DAppVersion=$Version" "/DSourceDir=$appDir" (Join-Path $root 'installer\RainetteMusic.iss')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $installer)) { throw 'Inno Setup did not produce RainetteMusicSetup.exe.' }
if ($RequireSigning) {
    & signtool sign /fd SHA256 /f $env:RAINETTE_CODESIGN_CERT_PATH /p $env:RAINETTE_CODESIGN_CERT_PASSWORD /tr http://timestamp.digicert.com /td SHA256 $installer
    if ($LASTEXITCODE -ne 0) { throw 'Installer signing failed.' }
    & signtool verify /pa /v $installer
    if ($LASTEXITCODE -ne 0) { throw 'Installer verification failed.' }
}

$hash = Write-Checksum $installer
@{
    version = $Version
    signed = [bool]$RequireSigning
    signatureVerified = [bool]$RequireSigning
    channel = $(if ($RequireSigning) { 'release' } else { 'local-test' })
    sha256 = $hash
    artifact = 'RainetteMusicSetup.exe'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'windows-release.json')
Write-Host "Built $installer"
