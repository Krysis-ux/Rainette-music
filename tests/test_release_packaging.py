import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _release_workflow() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def _build_macos_script() -> str:
    return (ROOT / "release" / "build-macos-release.sh").read_text(encoding="utf-8")


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


def test_windows_release_build_preserves_existing_artifacts():
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


def test_github_release_pipeline_splits_tests_platform_builds_and_publish_permissions():
    workflow = _release_workflow()
    test_job = _workflow_job(workflow, "test")
    windows_build_job = _workflow_job(workflow, "windows-build")
    macos_build_job = _workflow_job(workflow, "macos-build")
    windows_sign_job = _workflow_job(workflow, "windows-sign")
    publish_job = _workflow_job(workflow, "publish")

    assert re.search(r"(?m)^permissions:\s*\n  contents: read$", workflow)
    assert "runs-on: windows-latest" in test_job
    assert "python -m pytest" in test_job
    assert "node --check" in test_job
    assert "needs: test" in windows_build_job
    assert "runs-on: windows-latest" in windows_build_job
    # macOS builds on a real macOS runner: the bundle is produced by PyInstaller
    # via AppKit and packaged with ditto, neither of which cross-compiles.
    assert "needs: test" in macos_build_job
    assert "runs-on: macos-14" in macos_build_job
    assert "release/build-macos-release.sh --version \"${RELEASE_VERSION}\" --dmg" in macos_build_job
    # Both builds, because this one job signs both platforms' manifests and so
    # cannot start until each has produced one.
    assert "needs: [test, windows-build, macos-build]" in windows_sign_job
    assert "environment: release-signing" in windows_sign_job
    # Publishing waits on both platforms, so a half-built release is never cut.
    assert "needs: [windows-sign, macos-build]" in publish_job
    assert re.search(r"(?m)^    permissions:\s*\n      contents: write$", publish_job)
    assert workflow.count("contents: write") == 1


def test_macos_ships_a_signed_update_manifest_of_its_own():
    """macOS updates itself, and the trust for that costs nothing from Apple.

    The root of trust is the same Ed25519 manifest signature Windows uses, so
    the macOS manifest must be signed by the same isolated job rather than
    shipped unsigned -- an unsigned manifest is a file that only looks
    meaningful.  Notarisation is a separate concern: it removes Gatekeeper's
    prompt on a manual download and has no bearing on whether an update
    verifies.
    """
    workflow = _release_workflow()
    macos_build_job = _workflow_job(workflow, "macos-build")
    windows_sign_job = _workflow_job(workflow, "windows-sign")

    # The archive the updater downloads, and the manifest that pins its hash.
    assert "RainetteMusic-macOS.zip" in macos_build_job
    assert "latest-macos.json" in macos_build_job
    # ditto -ck, not zip -r: an .app archived without its symlinks and extended
    # attributes fails codesign on the other side, which the updater reads as a
    # tampered download.
    assert "ditto -ck --keepParent" in _build_macos_script()
    # Signed by the one credential-holding job, with the same reviewed script.
    assert "sign_manifest.py release/out-macos/latest-macos.json" in windows_sign_job
    # The bundle is verified as the artifact users actually receive.
    assert "codesign --verify" in macos_build_job
    assert "CFBundleShortVersionString" in macos_build_job
    # The image is mounted and inspected, because a .dmg that builds but mounts
    # empty -- or without the Applications symlink users drag onto -- is a
    # failure nothing else in this job would catch.
    assert "hdiutil attach" in macos_build_job
    assert 'codesign --verify --deep --strict "$MOUNT/Rainette Music.app"' in macos_build_job
    assert 'drag-to-install target' in macos_build_job


def test_macos_download_states_the_gatekeeper_step_it_requires():
    """An unnotarized build is reported as damaged, which needs explaining.

    Shipping it without the one command that clears quarantine leaves a user
    with an app that appears broken and no way to tell that it is not.
    """
    publish_job = _workflow_job(_release_workflow(), "publish")

    assert "xattr -dr com.apple.quarantine" in publish_job
    assert "not yet notarized" in publish_job
    # Updating in place needs the app to live somewhere writable: run straight
    # from the disk image, macOS translocates it to a read-only copy and the
    # swap cannot land. The notes have to say so, because the failure is
    # otherwise indistinguishable from the updater being broken.
    assert "from **Applications**" in publish_job
    assert "read-only copy" in publish_job


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


def test_github_publish_job_downloads_only_platform_job_artifacts():
    workflow = _release_workflow()
    publish_job = _workflow_job(workflow, "publish")

    # windows-build uploads the installer, macos-build the app bundle, and
    # windows-sign the installer manifest's signature.
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
        "RainetteMusicSetup.exe",
        "RainetteMusic-macOS.dmg",
        "latest.json",
        "latest.json.sig",
    ):
        assert filename in publish_job
    # Phones install the PWA from its own repository, so no phone artifact is
    # ever attached to a desktop release.
    assert "android" not in publish_job.lower()
    assert "files: release-assets/*" in publish_job
    assert "fail_on_unmatched_files: true" in publish_job


def test_github_actions_are_pinned_to_full_commit_shas():
    workflow = _release_workflow()
    action_refs = re.findall(r"(?m)^\s+uses:\s+([^\s#]+)", workflow)

    assert action_refs
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref in action_refs)


def test_dynamically_loading_packages_are_collected_on_both_platforms():
    """Packages that import part of themselves at run time need --collect-all.

    PyInstaller works from static analysis, so a package that reaches for its
    own submodules from inside a function body is invisible to it. mutagen is
    the sharp case behind this test: ``mutagen.File()`` imports twenty-odd
    format handlers from within its own body, so a bundle missing them reads
    tags perfectly in development and then returns None for every file once
    packaged — a failure that only ever shows up in the shipped app.

    Asserted for both builds together, because the two scripts are deliberately
    separate and drift is the normal failure here.
    """
    windows = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    macos = (ROOT / "release" / "build-macos-release.sh").read_text(encoding="utf-8")

    for package in ("webview", "ytmusicapi", "yt_dlp", "qrcode", "mutagen"):
        assert f"--collect-all {package}" in windows, f"{package} is not collected in the Windows build"
        assert f"--collect-all {package}" in macos, f"{package} is not collected in the macOS build"


def test_every_runtime_dependency_is_declared():
    """A module the app imports but requirements.txt never names installs on a
    developer's machine and is missing from a fresh checkout and from CI."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for package in ("aiohttp", "cryptography", "filelock", "yt-dlp", "ytmusicapi", "mutagen"):
        assert package in requirements, f"{package} is imported but not declared"
