from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_build_script_handles_spaced_root_and_propagates_pyinstaller_failure():
    payload = (ROOT / "build_exe.bat").read_bytes()
    text = payload.decode("ascii")
    assert '--distpath "%~dp0."' in text
    assert "if errorlevel 1 exit /b 1" in text.lower()
    assert "python -m PyInstaller" in text


def test_desktop_icon_is_a_real_windows_ico_not_a_renamed_png():
    icon = ROOT / "web" / "assets" / "rainette-icon.ico"
    payload = icon.read_bytes()
    assert payload[:4] == b"\x00\x00\x01\x00"
    frame_count = int.from_bytes(payload[4:6], "little")
    assert frame_count >= 4
