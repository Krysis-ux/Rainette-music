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


def test_inno_setup_wizard_installs_shortcuts_and_uninstaller_contract():
    installer = (ROOT / "installer" / "RainetteMusic.iss").read_text(encoding="utf-8")
    assert "WizardStyle=modern" in installer
    assert "{autopf}\\Rainette Music" in installer
    assert "{group}\\Rainette Music" in installer
    assert "{autodesktop}\\Rainette Music" in installer
    assert "OutputBaseFilename=RainetteMusicSetup" in installer


def test_android_release_requires_signing_and_verifies_apk():
    script = (ROOT / "mobile" / "build-release.ps1").read_text(encoding="utf-8")
    for variable in ("ANDROID_KEYSTORE_PATH", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD"):
        assert variable in script
    assert "Refusing to build a publish-ready Android release without publisher signing" in script
    assert "LocalTest" in script
    assert "verify --verbose" in script
    assert "rainette-music-android.apk" in script
