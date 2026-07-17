<#
Generate the Android release-signing keystore and print the five GitHub secrets
the release workflow needs to publish the APK.

Run this ONCE on a trusted machine (needs the JDK's keytool on PATH):

    powershell -ExecutionPolicy Bypass -File .\mobile\new_signing_key.ps1

Then, in the GitHub repo (Settings > Secrets and variables > Actions), add the
five secrets it prints. Back the .jks file up offline.

Android rule that makes the keystore permanent: once a phone installs an APK,
every future update MUST be signed with this SAME keystore. Android rejects an
over-the-top update signed by a different key. Lose it and existing installs
can never update (they'd have to uninstall and reinstall); anyone who signs with
it can push updates to those phones. Treat it like the Ed25519 private key.
#>

[CmdletBinding()]
param(
    [string]$OutputDir = (Join-Path $env:USERPROFILE 'rainette-android-signing'),
    [string]$Alias = 'rainette-release'
)

$ErrorActionPreference = 'Stop'

$keytool = (Get-Command 'keytool' -ErrorAction SilentlyContinue).Source
if (-not $keytool) { throw 'keytool was not found on PATH. Install a JDK (e.g. Temurin 21) and retry.' }

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$keystore = Join-Path $OutputDir 'rainette-release.jks'
if (Test-Path -LiteralPath $keystore) {
    throw "A keystore already exists at $keystore. Refusing to overwrite it (that would break updates for existing installs). Delete it deliberately if you truly want a new identity."
}

# Strong random password; the same value protects the store and the key.
$bytes = New-Object byte[] 24
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$password = [Convert]::ToBase64String($bytes).Replace('/', 'a').Replace('+', 'b').TrimEnd('=')

& $keytool -genkeypair -v -keystore $keystore -storepass $password -keypass $password `
    -alias $Alias -keyalg RSA -keysize 3072 -validity 10000 `
    -dname 'CN=Rainette Music, OU=Release, O=Rainette Music, L=Local, ST=Local, C=US'
if ($LASTEXITCODE -ne 0) { throw 'keytool failed to create the keystore.' }

$certOutput = & $keytool -list -v -keystore $keystore -storepass $password -alias $Alias
$fingerprint = ($certOutput | Select-String -Pattern 'SHA256:\s*([0-9A-Fa-f:]+)' |
    Select-Object -First 1).Matches.Groups[1].Value -replace '[:\s]', ''
$fingerprint = $fingerprint.ToLowerInvariant()
$keystoreBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($keystore))

Write-Host ''
Write-Host '=== Add these five GitHub Actions secrets ===' -ForegroundColor Green
Write-Host 'Settings > Secrets and variables > Actions > New repository secret'
Write-Host ''
Write-Host 'ANDROID_KEYSTORE_BASE64:'
Write-Host "  $keystoreBase64"
Write-Host ''
Write-Host "ANDROID_KEYSTORE_PASSWORD:  $password"
Write-Host "ANDROID_KEY_PASSWORD:       $password"
Write-Host "ANDROID_KEY_ALIAS:          $Alias"
Write-Host "ANDROID_SIGNING_CERT_SHA256: $fingerprint"
Write-Host ''
Write-Host "Keystore file: $keystore" -ForegroundColor Yellow
Write-Host 'BACK IT UP OFFLINE. Lose it = existing Android installs can never update.' -ForegroundColor Yellow
