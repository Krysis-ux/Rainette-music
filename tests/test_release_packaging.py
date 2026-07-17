import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _release_workflow() -> str:
    return (ROOT / ".github" / "workflows" / "android-release.yml").read_text(encoding="utf-8")


def _workflow_job(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        workflow,
    )
    assert match is not None, f"workflow job {name!r} is missing"
    return match.group(0)


def test_windows_release_build_is_self_contained_and_signing_guarded():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    assert "--onedir" in script
    assert '--add-data "$webDir;web"' in script
    assert "$webDir = Join-Path $root 'web'" in script
    assert "RAINETTE_CODESIGN_CERT_PATH" in script
    assert "RAINETTE_CODESIGN_CERT_PASSWORD" in script
    assert "signtool verify" in script
    assert "RainetteMusicSetup.exe" in script
    assert "ValidateSet('BuildUnsigned', 'SignAndPackage', 'Release', 'LocalTest')" in script


def test_windows_release_embeds_and_restores_the_public_update_signer_identity():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")

    assert "Normalize-SignerFingerprint" in script
    assert "^[0-9a-f]{64}$" in script
    assert "version.py" in script
    assert "UPDATE_SIGNER_CERT_SHA256" in script
    assert "[IO.File]::ReadAllBytes($releaseIdentity)" in script
    assert "[IO.File]::WriteAllBytes($releaseIdentity, $originalIdentityBytes)" in script
    rewrite = script.index("[IO.File]::WriteAllText($releaseIdentity, $embeddedIdentity, $utf8)")
    build = script.index("& $runPyInstaller", rewrite)
    restore = script.index("[IO.File]::WriteAllBytes($releaseIdentity, $originalIdentityBytes)", build)
    assert rewrite < build < restore


def test_windows_release_signing_phase_never_invokes_python_or_pyinstaller():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    signing_function = script[
        script.index("function Invoke-SignedPackage"):
        script.index("function Write-ReleaseManifest")
    ]
    signing_branch = script[
        script.rindex("    'SignAndPackage' {"):
        script.rindex("    'Release' {")
    ]

    assert "python" not in signing_function.lower()
    assert "pyinstaller" not in signing_function.lower()
    assert "Assert-SourceVersion" not in signing_branch
    assert "Invoke-SignedPackage" in signing_branch


def test_windows_release_phase_emits_a_schema2_manifest_for_the_ed25519_updater():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    # The updater trusts the manifest only after its Ed25519 signature verifies,
    # and the channel is what keeps LocalTest builds non-installable.
    assert "schema = 2" in script
    assert "-Channel 'release'" in script
    assert "-Channel 'local-test'" in script
    assert "sign_manifest.py" in script
    release_branch = script[
        script.rindex("    'Release' {"):
        script.rindex("    'LocalTest' {")
    ]
    assert "Assert-SourceVersion" in release_branch
    assert "Invoke-ReleasePackage" in release_branch


def test_manifest_signing_script_is_tiny_and_reads_the_key_from_the_environment():
    script = (ROOT / "release" / "sign_manifest.py").read_text(encoding="utf-8")
    # This is the only code the credential-holding CI job runs; it must stay
    # small enough to review at a glance and must never hardcode key material.
    assert "RAINETTE_UPDATE_SIGNING_KEY" in script
    assert "Ed25519PrivateKey" in script
    assert ".sig" in script
    assert len(script.splitlines()) < 80


def test_keygen_script_warns_about_key_custody():
    script = (ROOT / "release" / "new_signing_key.py").read_text(encoding="utf-8")
    assert "UPDATE_SIGNER_PUBLIC_KEY" in script
    assert "UPDATE_SIGNING_KEY" in script
    lowered = script.lower()
    assert "lose" in lowered and "leak" in lowered and "backup" in lowered.replace("back it up", "backup")


def test_windows_release_integrity_files_are_utf8_without_a_bom():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")

    assert "$utf8NoBom = [Text.UTF8Encoding]::new($false)" in script
    assert "[IO.File]::WriteAllText(" in script
    assert "[IO.File]::WriteAllText($buildMarker, $markerJson, $utf8NoBom)" in script
    assert "[IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8NoBom)" in script
    assert "Set-Content -LiteralPath $manifestPath" not in script


def test_webview_runtime_is_pinned_to_the_autoplay_patch_contract():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pywebview==6.2.1" in requirements


def test_windows_release_build_preserves_android_artifacts():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    assert "Remove-Item -LiteralPath $output -Recurse" not in script
    assert "$manifestPath = Join-Path $output 'latest.json'" in script


def test_inno_setup_wizard_installs_shortcuts_and_uninstaller_contract():
    installer = (ROOT / "installer" / "RainetteMusic.iss").read_text(encoding="utf-8")
    assert "WizardStyle=modern" in installer
    assert "{autopf}\\Rainette Music" in installer
    assert "{group}\\Rainette Music" in installer
    assert "{autodesktop}\\Rainette Music" in installer
    assert "OutputBaseFilename=RainetteMusicSetup" in installer


def test_inno_setup_scopes_companion_firewall_access_and_removes_it_on_uninstall():
    installer = (ROOT / "installer" / "RainetteMusic.iss").read_text(encoding="utf-8")
    assert 'name=""Rainette Music Companion""' in installer
    assert 'program=""{app}\\RainetteMusic.exe""' in installer
    assert "profile=private" in installer
    assert "protocol=TCP" in installer
    assert "remoteip=LocalSubnet" in installer
    assert "[UninstallRun]" in installer
    assert installer.count("advfirewall firewall delete rule") >= 2


def test_inno_setup_supports_the_in_app_updaters_silent_relaunch():
    installer = (ROOT / "installer" / "RainetteMusic.iss").read_text(encoding="utf-8")
    # The updater installs over the running app, so Inno must close it first to
    # release the file locks.
    assert "CloseApplications=yes" in installer
    # ...and relaunch only when the updater explicitly asks via /autorelaunch=1,
    # so an ordinary scripted /SILENT deploy does not surprise-launch the app.
    assert "WantsAutoRelaunch" in installer
    assert "{param:autorelaunch|0}" in installer


def test_windows_build_pins_runtime_version_to_the_release_version():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    # The updater compares version.APP_VERSION against the release tag, so the
    # build must refuse to package a mismatched constant.
    assert "version.APP_VERSION" in script
    assert "does not match -Version" in script


def test_android_release_requires_signing_and_verifies_apk():
    script = (ROOT / "mobile" / "build-release.ps1").read_text(encoding="utf-8")
    for variable in ("ANDROID_KEYSTORE_PATH", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD"):
        assert variable in script
    assert "Refusing to build a publish-ready Android release without publisher signing" in script
    assert "LocalTest" in script
    assert "verify --verbose" in script
    assert "rainette-music-android.apk" in script


def test_github_release_pipeline_splits_tests_platform_builds_and_publish_permissions():
    workflow = _release_workflow()
    test_job = _workflow_job(workflow, "test")
    android_job = _workflow_job(workflow, "android")
    windows_build_job = _workflow_job(workflow, "windows-build")
    windows_sign_job = _workflow_job(workflow, "windows-sign")
    publish_job = _workflow_job(workflow, "publish")

    assert re.search(r"(?m)^permissions:\s*\n  contents: read$", workflow)
    assert "runs-on: windows-latest" in test_job
    assert "python -m pytest" in test_job
    assert "node --check" in test_job
    assert "needs: test" in android_job
    assert "runs-on: ubuntu-latest" in android_job
    assert "needs: test" in windows_build_job
    assert "runs-on: windows-latest" in windows_build_job
    assert "needs: [test, windows-build]" in windows_sign_job
    assert "environment: release-signing" in windows_sign_job
    assert "needs: [android, windows-sign]" in publish_job
    assert re.search(r"(?m)^    permissions:\s*\n      contents: write$", publish_job)
    assert workflow.count("contents: write") == 1


def test_github_windows_release_is_built_signed_and_verified_from_the_tagged_checkout():
    workflow = _release_workflow()
    windows_build_job = _workflow_job(workflow, "windows-build")
    windows_sign_job = _workflow_job(workflow, "windows-sign")

    assert "actions/checkout@" in windows_build_job
    assert "ref: ${{ github.ref }}" in windows_build_job
    assert "persist-credentials: false" in windows_build_job
    assert "choco install innosetup --version=6.7.1" in windows_build_job
    assert "-Phase Release" in windows_build_job
    assert "rainette-windows-release" in windows_build_job
    # The build job never sees a credential of any kind.
    assert "secrets." not in windows_build_job
    assert "latest.json" in windows_build_job
    # The manifest gate: schema 2 + the release channel, or the updater refuses.
    assert "$manifest.schema -ne 2" in windows_build_job
    assert "'release'" in windows_build_job
    # The rebrand gate: an exe without its version resource would ship a bare
    # filename to Task Manager again.
    assert "FileDescription" in windows_build_job

    assert "actions/checkout@" in windows_sign_job
    assert "persist-credentials: false" in windows_sign_job
    assert "secrets.UPDATE_SIGNING_KEY" in windows_sign_job
    assert "sign_manifest.py" in windows_sign_job
    assert "latest.json.sig" in windows_sign_job
    # A signature CI produced but the app would refuse must fail the pipeline,
    # not brick the release: the signature is checked against the committed key.
    assert "UPDATE_SIGNER_PUBLIC_KEY" in windows_sign_job


def test_github_signing_credentials_only_enter_the_isolated_signing_job():
    workflow = _release_workflow()
    windows_build_job = _workflow_job(workflow, "windows-build")
    windows_sign_job = _workflow_job(workflow, "windows-sign")
    credential_step_name = "Sign the release manifest"
    credential_step = windows_sign_job[windows_sign_job.index(f"      - name: {credential_step_name}"):]
    before_credentials = windows_sign_job[:windows_sign_job.index(f"      - name: {credential_step_name}")]

    assert "environment: release-signing" in windows_sign_job
    # The signing job runs only the tiny reviewed signer plus its verification;
    # it never builds, packages, or executes the application.
    assert "PyInstaller" not in windows_sign_job
    assert "requirements.txt" not in windows_sign_job
    assert "cryptography" in windows_sign_job
    assert "secrets." not in before_credentials
    assert "secrets.UPDATE_SIGNING_KEY" in credential_step
    # The build job holds no secrets at all.
    assert "secrets." not in windows_build_job


def test_github_android_release_requires_the_production_certificate_identity():
    workflow = _release_workflow()
    android_job = _workflow_job(workflow, "android")

    assert "ANDROID_SIGNING_CERT_SHA256" in android_job
    assert "ref: ${{ github.ref }}" in android_job
    assert "release/out/RainetteMusicSetup.exe" not in android_job
    assert "apksigner verify" in android_job
    assert "Signer #1 certificate SHA-256 digest" in android_job
    assert "signatureVerified: true" in android_job
    assert "actions/upload-artifact@" in android_job
    assert "if: always()" in android_job


def test_github_publish_job_downloads_only_platform_job_artifacts():
    workflow = _release_workflow()
    publish_job = _workflow_job(workflow, "publish")

    assert workflow.count("actions/upload-artifact@") == 3
    assert "actions/download-artifact@" in publish_job
    assert "merge-multiple: true" in publish_job
    assert "softprops/action-gh-release@" in publish_job
    assert workflow.count("softprops/action-gh-release@") == 1
    assert "needs.test.outputs" not in publish_job
    assert "RELEASE_VERSION: ${{ github.ref_name }}" in publish_job
    # The exact-set verification gates the upload; every publishable asset must
    # be accounted for in the expected-set check.
    for filename in (
        "rainette-music-android.apk",
        "rainette-music-android.apk.sha256",
        "android-release.json",
        "RainetteMusicSetup.exe",
        "latest.json",
        "latest.json.sig",
    ):
        assert filename in publish_job
    assert "files: release-assets/*" in publish_job
    assert "fail_on_unmatched_files: true" in publish_job


def test_github_android_publishing_is_optional_but_windows_signing_is_not():
    workflow = _release_workflow()
    test_job = _workflow_job(workflow, "test")
    android_job = _workflow_job(workflow, "android")
    publish_job = _workflow_job(workflow, "publish")

    # The android job runs only when its signing secrets exist...
    assert "android_gate" in test_job
    assert "secrets.ANDROID_KEYSTORE_BASE64" in test_job
    assert "if: needs.test.outputs.android_enabled == 'true'" in android_job
    # ...and publish requires only the signed Windows release: a skipped OR
    # failed Android build must never block the Windows release.
    assert "needs.windows-sign.result == 'success'" in publish_job
    assert "needs.android.result == 'skipped'" not in publish_job
    # The APK ships only when the android job actually succeeded; the expected
    # asset set shrinks to Windows-only otherwise.
    assert 'if [ "${ANDROID_RESULT}" = "success" ]' in publish_job


def test_github_actions_are_pinned_to_full_commit_shas():
    workflow = _release_workflow()
    action_refs = re.findall(r"(?m)^\s+uses:\s+([^\s#]+)", workflow)

    assert action_refs
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref in action_refs)
