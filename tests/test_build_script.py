import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_make_version_file():
    spec = importlib.util.spec_from_file_location(
        "make_version_file", ROOT / "release" / "make_version_file.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_resource_brands_the_exe_for_task_manager():
    """Task Manager's Name column reads FileDescription; an empty one falls back
    to the filename (and a source run shows the interpreter's "Python"). The
    generated resource must carry the Rainette branding and the app version."""
    import version

    module = _load_make_version_file()
    rendered = module.render()

    assert "StringStruct('FileDescription', 'Rainette Music')" in rendered
    assert "StringStruct('ProductName', 'Rainette Music')" in rendered
    assert "StringStruct('CompanyName', 'Rainette Music')" in rendered
    assert "StringStruct('OriginalFilename', 'RainetteMusic.exe')" in rendered

    parts = list(version.parse_version(version.APP_VERSION))[:3]
    while len(parts) < 3:
        parts.append(0)
    assert f"filevers={(parts[0], parts[1], parts[2], 0)}" in rendered


def test_release_build_script_compiles_the_version_resource():
    """The PyInstaller build must feed the generated resource in via --version-file,
    otherwise the exe ships with the empty resource this branding fix removes."""
    script = (ROOT / "release" / "build-windows-release.ps1").read_text(encoding="utf-8")
    assert "make_version_file.py" in script
    assert "--version-file" in script


def test_desktop_icon_is_a_real_windows_ico_not_a_renamed_png():
    icon = ROOT / "web" / "assets" / "rainette-icon.ico"
    payload = icon.read_bytes()
    assert payload[:4] == b"\x00\x00\x01\x00"
    frame_count = int.from_bytes(payload[4:6], "little")
    assert frame_count >= 4
