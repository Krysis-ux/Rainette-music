from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_release_build_is_self_contained_and_signing_guarded():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    assert "--onedir" in script
    assert '--add-data "$webDir;web"' in script
    assert "$webDir = Join-Path $root 'web'" in script
    assert "RAINETTE_CODESIGN_CERT_PATH" in script
    assert "RAINETTE_CODESIGN_CERT_PASSWORD" in script
    assert "signtool verify" in script
    assert "RainetteMusicSetup.exe" in script


def test_windows_release_build_preserves_android_artifacts():
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    assert "Remove-Item -LiteralPath $output -Recurse" not in script
    assert '"$installer.sha256"' in script
    assert "(Join-Path $output 'windows-release.json')" in script


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
