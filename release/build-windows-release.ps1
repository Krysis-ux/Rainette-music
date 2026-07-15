[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [ValidateSet('BuildUnsigned', 'SignAndPackage', 'LocalTest')][string]$Phase = 'LocalTest',
    [string]$ExpectedSignerCertSha256 = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $PSScriptRoot 'stage'
$output = Join-Path $PSScriptRoot 'out'
$appDir = Join-Path $stage 'RainetteMusic'
$appExecutable = Join-Path $appDir 'RainetteMusic.exe'
$buildMarker = Join-Path $stage 'rainette-unsigned-build.json'
$installer = Join-Path $output 'RainetteMusicSetup.exe'
$manifestPath = Join-Path $output 'windows-release.json'
$webDir = Join-Path $root 'web'
$icon = Join-Path $webDir 'assets\rainette-icon.ico'
$entryPoint = Join-Path $root 'main.py'
$releaseIdentity = Join-Path $root 'release_identity.py'
$utf8NoBom = [Text.UTF8Encoding]::new($false)

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

function Normalize-SignerFingerprint([string]$Value) {
    $normalized = ($Value -replace '[:\s]', '').ToLowerInvariant()
    if ($normalized -notmatch '^[0-9a-f]{64}$') {
        throw 'ExpectedSignerCertSha256 must be exactly one SHA-256 certificate fingerprint (64 hexadecimal characters).'
    }
    return $normalized
}

function Write-Checksum([string]$Path) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText(
        "$Path.sha256",
        "$hash  $(Split-Path -Leaf $Path)",
        $utf8NoBom
    )
    return $hash
}

function Assert-SourceVersion {
    Require-Command 'python'
    # The updater compares the running app's version.APP_VERSION against the
    # release tag. This validation intentionally runs only in an uncredentialed
    # build phase, before a PFX or password exists in the process environment.
    $appVersion = (python -c "import version; print(version.normalize(version.APP_VERSION))").Trim()
    $buildVersion = (python -c "import version, sys; print(version.normalize(sys.argv[1]))" $Version).Trim()
    if ($appVersion -ne $buildVersion) {
        throw "version.APP_VERSION ($appVersion) does not match -Version ($buildVersion). Update version.py before building."
    }
}

function Clear-PackageOutputs {
    New-Item -ItemType Directory -Force $output | Out-Null
    foreach ($path in @($installer, "$installer.sha256", $manifestPath)) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-PyInstallerBuild([string]$EmbeddedSignerFingerprint) {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $stage, $output | Out-Null
    Clear-PackageOutputs

    $runPyInstaller = {
        Push-Location $root
        try {
            python -m PyInstaller --noconfirm --clean --onedir --noconsole --name RainetteMusic `
                --distpath $stage --workpath (Join-Path $stage 'work') --specpath (Join-Path $stage 'spec') `
                --icon $icon `
                --add-data "$webDir;web" --collect-all webview --collect-all ytmusicapi --collect-all yt_dlp --collect-all qrcode $entryPoint
            if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to build RainetteMusic.exe.' }
        } finally {
            Pop-Location
        }
    }

    if ($EmbeddedSignerFingerprint) {
        if (-not (Test-Path -LiteralPath $releaseIdentity -PathType Leaf)) {
            throw 'release_identity.py is required to pin the production update signer.'
        }
        $originalIdentityBytes = [IO.File]::ReadAllBytes($releaseIdentity)
        $utf8 = [Text.UTF8Encoding]::new($false, $true)
        $originalIdentity = $utf8.GetString($originalIdentityBytes)
        $identityPattern = '(?m)^UPDATE_SIGNER_CERT_SHA256\s*=\s*(?:""|'''')\s*$'
        $matches = [regex]::Matches($originalIdentity, $identityPattern)
        if ($matches.Count -ne 1) {
            throw 'release_identity.py must contain exactly one empty UPDATE_SIGNER_CERT_SHA256 assignment.'
        }
        $embeddedIdentity = [regex]::Replace(
            $originalIdentity,
            $identityPattern,
            "UPDATE_SIGNER_CERT_SHA256 = '$EmbeddedSignerFingerprint'"
        )
        try {
            [IO.File]::WriteAllText($releaseIdentity, $embeddedIdentity, $utf8)
            & $runPyInstaller
        } finally {
            # Restore the checked-in source byte-for-byte even when PyInstaller fails.
            [IO.File]::WriteAllBytes($releaseIdentity, $originalIdentityBytes)
        }
    } else {
        & $runPyInstaller
    }

    if (-not (Test-Path -LiteralPath $appExecutable -PathType Leaf)) {
        throw 'PyInstaller did not produce RainetteMusic.exe.'
    }

    $markerJson = @{
        version = $Version
        expectedSignerCertificateSha256 = $EmbeddedSignerFingerprint
    } | ConvertTo-Json
    [IO.File]::WriteAllText($buildMarker, $markerJson, $utf8NoBom)
}

function Assert-AuthenticodeSigner([string]$Path, [string]$ExpectedFingerprint) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate) {
        throw "Authenticode verification failed for $Path."
    }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $actualFingerprint = [BitConverter]::ToString(
            $hasher.ComputeHash($signature.SignerCertificate.RawData)
        ).Replace('-', '').ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
    if ($actualFingerprint -ne $ExpectedFingerprint) {
        throw "Unexpected Authenticode signer for $Path."
    }
}

function Invoke-InnoPackage([string]$InnoCompiler) {
    & $InnoCompiler "/DAppVersion=$Version" "/DSourceDir=$appDir" (Join-Path $root 'installer\RainetteMusic.iss')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw 'Inno Setup did not produce RainetteMusicSetup.exe.'
    }
}

function Invoke-SignedPackage([string]$ExpectedFingerprint) {
    foreach ($name in 'RAINETTE_CODESIGN_CERT_PATH', 'RAINETTE_CODESIGN_CERT_PASSWORD') {
        if (-not (Get-Item -Path "env:$name" -ErrorAction SilentlyContinue).Value) {
            throw "$name is required for the isolated signing and packaging phase."
        }
    }
    if (-not (Test-Path -LiteralPath $env:RAINETTE_CODESIGN_CERT_PATH -PathType Leaf)) {
        throw 'RAINETTE_CODESIGN_CERT_PATH does not identify a code-signing certificate file.'
    }
    if (-not (Test-Path -LiteralPath $appExecutable -PathType Leaf)) {
        throw 'The unsigned RainetteMusic.exe build is missing.'
    }
    if (-not (Test-Path -LiteralPath $buildMarker -PathType Leaf)) {
        throw 'The unsigned build identity marker is missing.'
    }
    $marker = Get-Content -LiteralPath $buildMarker -Raw | ConvertFrom-Json
    if ($marker.version -ne $Version -or $marker.expectedSignerCertificateSha256 -ne $ExpectedFingerprint) {
        throw 'The unsigned application was not built for this version and signer identity.'
    }

    Require-Command 'signtool'
    $iscc = Find-InnoCompiler
    Clear-PackageOutputs

    & signtool sign /fd SHA256 /f $env:RAINETTE_CODESIGN_CERT_PATH /p $env:RAINETTE_CODESIGN_CERT_PASSWORD /tr http://timestamp.digicert.com /td SHA256 $appExecutable
    if ($LASTEXITCODE -ne 0) { throw 'Application Authenticode signing failed.' }
    & signtool verify /pa /v $appExecutable
    if ($LASTEXITCODE -ne 0) { throw 'Application Authenticode verification failed.' }
    Assert-AuthenticodeSigner $appExecutable $ExpectedFingerprint

    Invoke-InnoPackage $iscc
    & signtool sign /fd SHA256 /f $env:RAINETTE_CODESIGN_CERT_PATH /p $env:RAINETTE_CODESIGN_CERT_PASSWORD /tr http://timestamp.digicert.com /td SHA256 $installer
    if ($LASTEXITCODE -ne 0) { throw 'Installer Authenticode signing failed.' }
    & signtool verify /pa /v $installer
    if ($LASTEXITCODE -ne 0) { throw 'Installer Authenticode verification failed.' }
    Assert-AuthenticodeSigner $installer $ExpectedFingerprint

    $hash = Write-Checksum $installer
    $manifestJson = @{
        version = $Version
        signed = $true
        signatureVerified = $true
        channel = 'release'
        sha256 = $hash
        artifact = 'RainetteMusicSetup.exe'
        signerCertificateSha256 = $ExpectedFingerprint
    } | ConvertTo-Json
    [IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)
}

function Invoke-LocalTestPackage {
    $iscc = Find-InnoCompiler
    Clear-PackageOutputs
    Invoke-InnoPackage $iscc
    $hash = Write-Checksum $installer
    $manifestJson = @{
        version = $Version
        signed = $false
        signatureVerified = $false
        channel = 'local-test'
        sha256 = $hash
        artifact = 'RainetteMusicSetup.exe'
    } | ConvertTo-Json
    [IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)
}

switch ($Phase) {
    'BuildUnsigned' {
        Assert-SourceVersion
        $expectedSigner = Normalize-SignerFingerprint $ExpectedSignerCertSha256
        Invoke-PyInstallerBuild $expectedSigner
        Write-Host "Built unsigned application at $appExecutable"
    }
    'SignAndPackage' {
        $expectedSigner = Normalize-SignerFingerprint $ExpectedSignerCertSha256
        Invoke-SignedPackage $expectedSigner
        Write-Host "Built signed installer at $installer"
    }
    'LocalTest' {
        Assert-SourceVersion
        Invoke-PyInstallerBuild ''
        Invoke-LocalTestPackage
        Write-Host "Built unsigned local-test installer at $installer"
    }
}
