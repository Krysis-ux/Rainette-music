[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [ValidateSet('BuildUnsigned', 'SignAndPackage', 'Release', 'LocalTest')][string]$Phase = 'LocalTest',
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
$manifestPath = Join-Path $output 'latest.json'
$webDir = Join-Path $root 'web'
$icon = Join-Path $webDir 'assets\rainette-icon.ico'
$entryPoint = Join-Path $root 'main.py'
$releaseIdentity = Join-Path $root 'version.py'
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
    foreach ($path in @($installer, $manifestPath)) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-PyInstallerBuild([string]$EmbeddedSignerFingerprint) {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $stage, $output | Out-Null
    Clear-PackageOutputs

    # Compile a Win32 version resource from version.APP_VERSION so Task Manager and
    # the exe's Properties dialog read "Rainette Music" instead of an empty string.
    $versionFile = Join-Path $stage 'rainette-version-info.txt'
    python (Join-Path $PSScriptRoot 'make_version_file.py') $versionFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
        throw 'Failed to generate the Windows version resource.'
    }

    # Every --collect-all here names a package that loads part of itself at run
    # time, where PyInstaller's static analysis cannot follow. mutagen is the
    # sharpest case: mutagen.File() imports twenty-odd format modules from
    # inside its own body, so a bundle without them reads tags fine in
    # development and returns None for every file once packaged.
    $runPyInstaller = {
        Push-Location $root
        try {
            python -m PyInstaller --noconfirm --clean --onedir --noconsole --name RainetteMusic `
                --distpath $stage --workpath (Join-Path $stage 'work') --specpath (Join-Path $stage 'spec') `
                --icon $icon --version-file $versionFile `
                --add-data "$webDir;web" --collect-all webview --collect-all ytmusicapi --collect-all yt_dlp --collect-all qrcode --collect-all mutagen $entryPoint
            if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to build RainetteMusic.exe.' }
        } finally {
            Pop-Location
        }
    }

    if ($EmbeddedSignerFingerprint) {
        if (-not (Test-Path -LiteralPath $releaseIdentity -PathType Leaf)) {
            throw 'version.py is required to pin the production update signer.'
        }
        $originalIdentityBytes = [IO.File]::ReadAllBytes($releaseIdentity)
        $utf8 = [Text.UTF8Encoding]::new($false, $true)
        $originalIdentity = $utf8.GetString($originalIdentityBytes)
        $identityPattern = '(?m)^UPDATE_SIGNER_CERT_SHA256\s*=\s*(?:""|'''')\s*$'
        $matches = [regex]::Matches($originalIdentity, $identityPattern)
        if ($matches.Count -ne 1) {
            throw 'version.py must contain exactly one empty UPDATE_SIGNER_CERT_SHA256 assignment.'
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

    Write-ReleaseManifest -Channel 'release' -AuthenticodeSigned $true -SignerCertSha256 $ExpectedFingerprint
}

# Schema-2 manifest: the updater trusts these fields only after the manifest's
# detached Ed25519 signature (latest.json.sig, produced by
# release/sign_manifest.py) verifies against the key in version.py.
# `channel` is what keeps LocalTest builds non-installable even if published.
function Write-ReleaseManifest([string]$Channel, [bool]$AuthenticodeSigned, [string]$SignerCertSha256 = '') {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    $manifestJson = @{
        schema = 2
        version = $Version
        channel = $Channel
        artifact = 'RainetteMusicSetup.exe'
        sha256 = $hash
        authenticode = @{
            signed = $AuthenticodeSigned
            signerCertificateSha256 = $SignerCertSha256
        }
    } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)
}

function Invoke-ReleasePackage {
    $iscc = Find-InnoCompiler
    Clear-PackageOutputs
    Invoke-InnoPackage $iscc
    Write-ReleaseManifest -Channel 'release' -AuthenticodeSigned $false
}

function Invoke-LocalTestPackage {
    $iscc = Find-InnoCompiler
    Clear-PackageOutputs
    Invoke-InnoPackage $iscc
    Write-ReleaseManifest -Channel 'local-test' -AuthenticodeSigned $false
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
    'Release' {
        # The certless release path: package + checksum + schema-2 manifest.
        # CI signs the manifest afterwards with release/sign_manifest.py in an
        # isolated job holding the UPDATE_SIGNING_KEY secret.
        Assert-SourceVersion
        Invoke-PyInstallerBuild ''
        Invoke-ReleasePackage
        Write-Host "Built release installer at $installer (sign the manifest with release/sign_manifest.py)"
    }
    'LocalTest' {
        Assert-SourceVersion
        Invoke-PyInstallerBuild ''
        Invoke-LocalTestPackage
        Write-Host "Built unsigned local-test installer at $installer"
    }
}
