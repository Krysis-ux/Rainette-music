"""Tests for the managed HTTPS tunnel and the pairing addresses it produces.

The failure these cover is the one users actually hit: a pairing code minted
while the computer had no public address carries ``http://127.0.0.1:<port>``,
which a phone can never call, and the browser reports it only as "Failed to
fetch" / "Load failed".
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
import tunnel

TERMINAL_PHASES = {"running", "error", "stopped"}


class FakeProcess:
    """Stand-in for cloudflared: emits log lines, then stays alive."""

    def __init__(self, lines: list[str], *, exit_code: int | None = None) -> None:
        self.stdout = iter(lines)
        self.terminated = False
        self._exit_code = exit_code

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self._exit_code = 0

    def wait(self, timeout: float | None = None) -> int:
        self._exit_code = 0 if self._exit_code is None else self._exit_code
        return self._exit_code

    def kill(self) -> None:
        self.terminate()


QUICK_TUNNEL_LOG = [
    "INF Thank you for trying Cloudflare Tunnel.\n",
    "INF +---------------------------------------+\n",
    "INF |  https://calm-frog-mixes.trycloudflare.com  |\n",
    "INF +---------------------------------------+\n",
]


@pytest.fixture
def fast_tunnel(monkeypatch):
    """Collapse the real-world waits so the state machine can be exercised."""
    monkeypatch.setattr(tunnel, "_URL_DISCOVERY_TIMEOUT_S", 3.0)
    monkeypatch.setattr(tunnel, "_REACHABLE_TIMEOUT_S", 2.0)
    monkeypatch.setattr(tunnel, "_PROBE_INTERVAL_S", 0.05)


def settle(manager: tunnel.TunnelManager, *, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = manager.status()
        if status["phase"] in TERMINAL_PHASES:
            return status
        time.sleep(0.02)
    raise AssertionError(f"tunnel never settled: {manager.status()}")


def build_manager(
    tmp_path,
    monkeypatch,
    process,
    *,
    reachable=lambda url: True,
    on_url=None,
    helper=Path("cloudflared"),
    installer=None,
):
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *args, **kwargs: process)
    return tunnel.TunnelManager(
        tmp_path,
        on_url=on_url,
        binary_locator=lambda: helper,
        binary_installer=installer or (lambda progress: Path("cloudflared")),
        reachable_probe=reachable,
    )


# ── the tunnel state machine ──────────────────────────────────────────────


def test_quick_tunnel_address_is_discovered_and_published(tmp_path, monkeypatch, fast_tunnel):
    # Arrange
    published: list[str] = []
    manager = build_manager(
        tmp_path, monkeypatch, FakeProcess(QUICK_TUNNEL_LOG), on_url=published.append
    )

    # Act
    manager.start(47811)
    status = settle(manager)

    # Assert
    assert status["phase"] == "running"
    assert status["url"] == "https://calm-frog-mixes.trycloudflare.com"
    assert status["port"] == 47811
    assert published == ["https://calm-frog-mixes.trycloudflare.com"]
    manager.stop()


def test_missing_tunnel_address_is_reported_as_an_error(tmp_path, monkeypatch, fast_tunnel):
    # Arrange: cloudflared that never announces a hostname.
    manager = build_manager(tmp_path, monkeypatch, FakeProcess(["INF starting\n"]))

    # Act
    manager.start(47811)
    status = settle(manager)

    # Assert
    assert status["phase"] == "error"
    assert not status["url"]


def test_address_that_never_answers_is_reported_as_an_error(tmp_path, monkeypatch, fast_tunnel):
    # Arrange: Cloudflare hands out a hostname that never routes back to us.
    manager = build_manager(
        tmp_path, monkeypatch, FakeProcess(QUICK_TUNNEL_LOG), reachable=lambda url: False
    )

    # Act
    manager.start(47811)
    status = settle(manager)

    # Assert
    assert status["phase"] == "error"
    assert not status["url"]


def test_stop_terminates_the_helper_process(tmp_path, monkeypatch, fast_tunnel):
    # Arrange
    process = FakeProcess(QUICK_TUNNEL_LOG)
    manager = build_manager(tmp_path, monkeypatch, process)
    manager.start(47811)
    settle(manager)

    # Act
    status = manager.stop()

    # Assert
    assert process.terminated is True
    assert status["phase"] == "stopped"
    assert not status["url"]


def test_generating_a_tunnel_requires_the_helper_to_be_downloaded_first(tmp_path, monkeypatch):
    # Arrange: a fresh install, before the user has pressed "Download cloudflared".
    manager = build_manager(tmp_path, monkeypatch, FakeProcess([]), helper=None)

    # Act / Assert
    with pytest.raises(tunnel.TunnelError):
        manager.start(47811)
    assert manager.status()["phase"] == "stopped"


def test_helper_status_reports_a_missing_binary(tmp_path, monkeypatch):
    manager = build_manager(tmp_path, monkeypatch, FakeProcess([]), helper=None)

    helper = manager.helper_status()

    assert helper["phase"] == "missing"
    assert helper["ready"] is False


def test_downloading_the_helper_reports_ready_when_it_lands(tmp_path, monkeypatch):
    # Arrange
    installed = tmp_path / "tools" / "cloudflared"
    manager = build_manager(
        tmp_path,
        monkeypatch,
        FakeProcess([]),
        helper=None,
        installer=lambda progress: installed,
    )

    # Act
    manager.download_helper()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and manager.helper_status()["busy"]:
        time.sleep(0.02)

    # Assert
    helper = manager.helper_status()
    assert helper["phase"] == "ready"
    assert helper["ready"] is True
    assert helper["path"] == str(installed)


def test_a_failed_helper_download_is_reported_rather_than_raised(tmp_path, monkeypatch):
    # Arrange
    def broken(progress):
        raise tunnel.TunnelError("could not download the tunnel helper: offline")

    manager = build_manager(
        tmp_path, monkeypatch, FakeProcess([]), helper=None, installer=broken
    )

    # Act
    manager.download_helper()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and manager.helper_status()["busy"]:
        time.sleep(0.02)

    # Assert
    helper = manager.helper_status()
    assert helper["phase"] == "error"
    assert "offline" in helper["message"]


def test_start_rejects_a_port_outside_the_valid_range(tmp_path, monkeypatch):
    manager = build_manager(tmp_path, monkeypatch, FakeProcess([]))
    with pytest.raises(ValueError):
        manager.start(0)


# ── downloading the helper ────────────────────────────────────────────────


def test_download_refuses_a_redirect_that_leaves_the_release_channel():
    handler = tunnel._PinnedRedirectHandler()
    with pytest.raises(tunnel.TunnelError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/evil.exe")


def test_download_refuses_a_non_https_source():
    with pytest.raises(tunnel.TunnelError):
        tunnel._open_release_asset("http://github.com/cloudflare/cloudflared/releases/x")


def test_only_the_official_release_hosts_are_accepted():
    assert tunnel._host_allowed("https://github.com/cloudflare/cloudflared/releases/latest")
    assert tunnel._host_allowed("https://objects.githubusercontent.com/asset")
    assert not tunnel._host_allowed("https://github.com.example.net/asset")
    assert not tunnel._host_allowed("https://example.com/asset")


def test_platform_asset_is_named_for_the_running_system():
    asset = tunnel._asset_name()
    assert asset.startswith("cloudflared-")
    assert any(system in asset for system in ("windows", "darwin", "linux"))


# ── reachability probe ────────────────────────────────────────────────────


def test_probe_accepts_an_unauthenticated_gateway_response(monkeypatch):
    # 401 comes from Rainette's own auth middleware, so it proves the whole
    # path — tunnel, edge, loopback listener — is wired up.
    def unauthorized(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(tunnel.urllib.request, "urlopen", unauthorized)
    assert tunnel._probe_reachable("https://calm-frog-mixes.trycloudflare.com") is True


def test_probe_rejects_a_cloudflare_edge_error(monkeypatch):
    def bad_gateway(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(tunnel.urllib.request, "urlopen", bad_gateway)
    assert tunnel._probe_reachable("https://calm-frog-mixes.trycloudflare.com") is False


def test_probe_rejects_an_unreachable_host(monkeypatch):
    def refused(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(tunnel.urllib.request, "urlopen", refused)
    assert tunnel._probe_reachable("https://calm-frog-mixes.trycloudflare.com") is False


# ── the addresses pairing links are built from ────────────────────────────


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "APP_DATA_DIR", tmp_path)
    return tmp_path


def test_invitation_flags_a_loopback_endpoint(isolated_config, monkeypatch):
    # Arrange: no public address configured, which is the state every fresh
    # install starts in.
    monkeypatch.setattr(server, "start_companion", lambda: {"port": 47811})

    # Act
    invitation = server.create_companion_invitation()

    # Assert
    assert invitation["endpoint"] == "http://127.0.0.1:47811"
    assert invitation["endpoint_is_local"] is True
    assert invitation["tunnel_configured"] is False
    assert invitation["companion_port"] == 47811


def test_invitation_uses_the_configured_public_address(isolated_config, monkeypatch):
    # Arrange
    monkeypatch.setattr(server, "start_companion", lambda: {"port": 47811})
    server.write_pwa_config("https://music-pwa-web.vercel.app", "https://calm-frog-mixes.trycloudflare.com")

    # Act
    invitation = server.create_companion_invitation()

    # Assert
    assert invitation["endpoint"] == "https://calm-frog-mixes.trycloudflare.com"
    assert invitation["endpoint_is_local"] is False
    assert invitation["tunnel_configured"] is True
    assert invitation["endpoint"] in invitation["pairing_url"].replace("%3A", ":").replace("%2F", "/")


def test_only_a_generated_address_is_recognised_as_managed():
    assert server.is_managed_tunnel_url("https://calm-frog-mixes.trycloudflare.com") is True
    assert server.is_managed_tunnel_url("https://music-pc.example.com") is False
    assert server.is_managed_tunnel_url("") is False


def test_setting_the_public_address_keeps_the_pwa_address(isolated_config):
    # Arrange
    server.write_pwa_config("https://rainette-preview.example.com", "")

    # Act
    config = server.set_public_url("https://calm-frog-mixes.trycloudflare.com")

    # Assert
    assert config["pwa_url"] == "https://rainette-preview.example.com"
    assert config["public_url"] == "https://calm-frog-mixes.trycloudflare.com"


def test_stopping_the_tunnel_clears_only_an_address_rainette_minted(isolated_config, monkeypatch):
    # Arrange: a user-supplied named tunnel must survive a stop.
    monkeypatch.setattr(server.tunnel_manager(), "stop", lambda **kwargs: {"phase": "stopped"})
    server.write_pwa_config("https://music-pwa-web.vercel.app", "https://music-pc.example.com")

    # Act
    server.stop_tunnel()

    # Assert
    assert server.read_pwa_config()["public_url"] == "https://music-pc.example.com"

    # Act: a generated Quick Tunnel address is dropped, because it dies with the
    # process and would otherwise be baked into unusable pairing links.
    server.write_pwa_config("https://music-pwa-web.vercel.app", "https://calm-frog-mixes.trycloudflare.com")
    server.stop_tunnel()

    # Assert
    assert server.read_pwa_config()["public_url"] == ""


def test_a_plain_http_public_address_is_refused(isolated_config):
    # An HTTPS page cannot call a plain-HTTP endpoint on another device, so this
    # has to fail at configuration time rather than at pairing time.
    with pytest.raises(ValueError):
        server.write_pwa_config("https://music-pwa-web.vercel.app", "http://192.168.1.20:47811")


def test_tunnel_status_reports_the_address_pairing_would_use(isolated_config):
    # Arrange
    server.write_pwa_config("https://music-pwa-web.vercel.app", "https://calm-frog-mixes.trycloudflare.com")

    # Act
    status = server.tunnel_status()

    # Assert
    assert status["public_url"] == "https://calm-frog-mixes.trycloudflare.com"
    assert status["public_url_is_managed"] is True
    assert status["phase"] == "stopped"
    assert status["helper"]["phase"] in {"missing", "ready", "error"}


def test_pwa_config_survives_a_damaged_file(isolated_config):
    # Arrange
    (isolated_config / server.PWA_CONFIG_FILENAME).write_text("{not json", encoding="utf-8")

    # Act
    config = server.read_pwa_config()

    # Assert: a corrupt optional file must never stop Rainette from starting.
    assert config["pwa_url"] == server.DEFAULT_PWA_URL
    assert config["public_url"] == ""


def test_written_config_is_valid_json(isolated_config):
    server.write_pwa_config("https://music-pwa-web.vercel.app", "https://calm-frog-mixes.trycloudflare.com")
    payload = json.loads((isolated_config / server.PWA_CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert payload == {
        "pwa_url": "https://music-pwa-web.vercel.app",
        "public_url": "https://calm-frog-mixes.trycloudflare.com",
    }
