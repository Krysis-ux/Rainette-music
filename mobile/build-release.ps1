[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [ValidateRange(1, 2100000000)][int]$VersionCode = 1,
    [switch]$LocalTest,
    [switch]$UseWindowsCertificateStore,
    [string]$AndroidSdkPath = $env:ANDROID_HOME,
    [string]$JavaHome = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$android = Join-Path $PSScriptRoot 'android'
$output = Join-Path $root 'release\out'

function Require-Command([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) { throw "$Name is required but was not found on PATH." }
    return $command.Source
}

function Find-Keytool([string]$RequestedJavaHome) {
    if ($RequestedJavaHome) {
        $candidate = Join-Path $RequestedJavaHome 'bin\keytool.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return Require-Command 'keytool.exe'
}

function Find-JavaHome([string]$RequestedJavaHome) {
    $rainetteTools = Join-Path $env:LOCALAPPDATA 'Programs\Rainette Build Tools'
    $localJdk = if (Test-Path -LiteralPath $rainetteTools) {
        (Get-ChildItem -LiteralPath $rainetteTools -Directory |
            Where-Object { $_.Name -like 'jdk-21*' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe')) } |
            Sort-Object Name -Descending |
            Select-Object -First 1).FullName
    }
    foreach ($candidate in @($RequestedJavaHome, $localJdk, $env:JAVA_HOME)) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'bin\java.exe'))) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            $releaseFile = Join-Path $resolved 'release'
            if ((Test-Path -LiteralPath $releaseFile) -and (Get-Content -LiteralPath $releaseFile -Raw) -match 'JAVA_VERSION="21[\.]') {
                return $resolved
            }
        }
    }
    throw 'JDK 21 is required. Pass -JavaHome or install it under %LOCALAPPDATA%\Programs\Rainette Build Tools.'
}

function Find-AndroidSdk([string]$RequestedPath) {
    foreach ($candidate in @(
        $RequestedPath,
        $env:ANDROID_SDK_ROOT,
        (Join-Path $env:LOCALAPPDATA 'Android\Sdk')
    )) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'build-tools'))) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Android SDK build tools were not found. Set ANDROID_HOME or pass -AndroidSdkPath.'
}

function New-LocalSigningIdentity([string]$Keytool) {
    $signingDir = Join-Path $env:LOCALAPPDATA 'Rainette Music\signing'
    $keystore = Join-Path $signingDir 'rainette-local-test.jks'
    $secretFile = Join-Path $signingDir 'local-test.secret'
    New-Item -ItemType Directory -Force -Path $signingDir | Out-Null

    if (Test-Path -LiteralPath $secretFile) {
        $secret = (Get-Content -LiteralPath $secretFile -Raw).Trim()
    } else {
        $bytes = New-Object byte[] 24
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
        $secret = [Convert]::ToBase64String($bytes).Replace('/', 'a').Replace('+', 'b').TrimEnd('=')
        Set-Content -LiteralPath $secretFile -Value $secret -NoNewline
    }

    if (-not (Test-Path -LiteralPath $keystore)) {
        & $Keytool -genkeypair -v -keystore $keystore -storepass $secret -keypass $secret `
            -alias rainette-local-test -keyalg RSA -keysize 3072 -validity 3650 `
            -dname 'CN=Rainette Music Local Test, OU=Development, O=Rainette Music, L=Local, ST=Local, C=US'
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Android test signing identity.' }
    }

    return @{
        Path = $keystore
        Password = $secret
        Alias = 'rainette-local-test'
    }
}

Require-Command 'npm.cmd' | Out-Null
$sdk = Find-AndroidSdk $AndroidSdkPath
$JavaHome = Find-JavaHome $JavaHome
$keytool = Find-Keytool $JavaHome

if ($JavaHome) {
    $env:JAVA_HOME = (Resolve-Path -LiteralPath $JavaHome).Path
    $env:Path = "$(Join-Path $env:JAVA_HOME 'bin');$env:Path"
}
if ($UseWindowsCertificateStore) {
    $windowsTrust = '-Djavax.net.ssl.trustStore=NONE -Djavax.net.ssl.trustStoreType=Windows-ROOT'
    $env:GRADLE_OPTS = "$windowsTrust $env:GRADLE_OPTS".Trim()
}
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
$env:RAINETTE_VERSION_NAME = $Version
$env:RAINETTE_VERSION_CODE = [string]$VersionCode

if ($LocalTest) {
    $identity = New-LocalSigningIdentity $keytool
    $env:ANDROID_KEYSTORE_PATH = $identity.Path
    $env:ANDROID_KEYSTORE_PASSWORD = $identity.Password
    $env:ANDROID_KEY_ALIAS = $identity.Alias
    $env:ANDROID_KEY_PASSWORD = $identity.Password
    $trustedPublisherSignature = $false
    $channel = 'local-test'
} else {
    foreach ($name in 'ANDROID_KEYSTORE_PATH', 'ANDROID_KEYSTORE_PASSWORD', 'ANDROID_KEY_ALIAS', 'ANDROID_KEY_PASSWORD') {
        $value = (Get-Item -Path "env:$name" -ErrorAction SilentlyContinue).Value
        if (-not $value) { throw "$name is required. Refusing to build a publish-ready Android release without publisher signing." }
    }
    if (-not (Test-Path -LiteralPath $env:ANDROID_KEYSTORE_PATH)) { throw 'ANDROID_KEYSTORE_PATH does not exist.' }
    $trustedPublisherSignature = $true
    $channel = 'release'
}

Push-Location $PSScriptRoot
try {
    & npm.cmd run sync
    if ($LASTEXITCODE -ne 0) { throw 'Capacitor sync failed.' }
} finally { Pop-Location }

Push-Location $android
try {
    & .\gradlew.bat assembleRelease
    if ($LASTEXITCODE -ne 0) { throw 'Android release build failed.' }
} finally { Pop-Location }

$apksigner = Get-ChildItem -Path (Join-Path $sdk 'build-tools') -Filter 'apksigner.bat' -Recurse |
    Sort-Object FullName |
    Select-Object -Last 1
if (-not $apksigner) { throw 'apksigner.bat was not found under the Android SDK.' }

$source = Join-Path $android 'app\build\outputs\apk\release\app-release.apk'
if (-not (Test-Path -LiteralPath $source)) { throw 'Signed app-release.apk was not created.' }
& $apksigner.FullName verify --verbose --print-certs $source
if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed.' }

New-Item -ItemType Directory -Force -Path $output | Out-Null
$artifact = Join-Path $output 'rainette-music-android.apk'
Copy-Item -LiteralPath $source -Destination $artifact -Force
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$artifact.sha256" -Value "$hash  rainette-music-android.apk" -NoNewline
@{
    version = $Version
    versionCode = $VersionCode
    signed = $trustedPublisherSignature
    signatureVerified = $true
    channel = $channel
    sha256 = $hash
    artifact = 'rainette-music-android.apk'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'android-release.json')

if ($LocalTest) {
    Write-Warning 'Built a cryptographically signed LOCAL TEST APK. Its certificate is not the Rainette public release identity.'
}
Write-Host "Built $artifact"
