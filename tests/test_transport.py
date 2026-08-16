"""Tests for the transport provider seam.

The property under test throughout is that adding options did not remove the
one that already worked.  A user who never opens the new picker must get the
Cloudflare Quick Tunnel, on a fresh install and on an install that predates the
seam entirely, and every new provider must report unfinished setup as a
checklist rather than as a failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transport
import tunnel

TERMINAL_PHASES = {"running", "error", "stopped", "setup"}


class FakeProcess:
    """Stand-in for a helper binary: emits log lines, then stays alive."""

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
    "INF |  https://calm-frog-mixes.trycloudflare.com  |\n",
]


@pytest.fixture
def fast_tunnel(monkeypatch):
    """Collapse the real-world waits so the state machine can be exercised."""
    monkeypatch.setattr(tunnel, "_URL_DISCOVERY_TIMEOUT_S", 3.0)
    monkeypatch.setattr(tunnel, "_REACHABLE_TIMEOUT_S", 2.0)
    monkeypatch.setattr(tunnel, "_REGISTRATION_TIMEOUT_S", 0.2)
    monkeypatch.setattr(tunnel, "_REACHABLE_GRACE_S", 0.1)
    monkeypatch.setattr(tunnel, "_PROBE_INTERVAL_S", 0.05)
    monkeypatch.setattr(tunnel, "_PROBE_INTERVAL_MAX_S", 0.1)


def settle(manager: tunnel.TunnelManager, *, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = manager.status()
        if status["phase"] in TERMINAL_PHASES:
            return status
        time.sleep(0.02)
    raise AssertionError(f"transport never settled: {manager.status()}")


# ── selecting a provider ──────────────────────────────────────────────────


def test_an_unconfigured_install_resolves_to_the_quick_tunnel(tmp_path):
    """The one option that needs no setup stays the one you get by default."""
    # Act
    selection = transport.read_selection(tmp_path)

    # Assert
    assert selection.provider == "cloudflare-quick"
    assert selection.config == {}


def test_the_manager_of_an_unconfigured_install_uses_the_quick_tunnel(tmp_path):
    # Arrange
    manager = tunnel.TunnelManager(tmp_path, binary_locator=lambda: Path("cloudflared"))

    # Act
    status = manager.status()

    # Assert
    assert status["provider"] == "cloudflare-quick"
    assert status["provider_label"] == "Limited tunnel"
    assert status["stable_hostname"] is False


def test_every_advertised_provider_can_be_built(tmp_path):
    # Arrange
    catalogue = transport.catalogue()

    # Assert
    assert [entry["id"] for entry in catalogue] == list(transport.PROVIDER_IDS)
    for entry in catalogue:
        provider = transport.build_provider(entry["id"])
        assert provider.id == entry["id"]
        assert provider.label == entry["label"]


def test_the_labels_the_settings_page_shows_are_the_agreed_ones():
    labels = {entry["id"]: entry["label"] for entry in transport.catalogue()}
    assert labels["cloudflare-quick"] == "Limited tunnel"
    assert labels["tailscale-serve"] == "Private link"
    assert labels["tailscale-funnel"] == "High-quality tunnel"


def test_only_the_private_link_is_kept_off_the_public_internet():
    capabilities = {entry["id"]: entry for entry in transport.catalogue()}
    assert capabilities["tailscale-serve"]["public"] is False
    assert capabilities["tailscale-funnel"]["public"] is True
    assert capabilities["cloudflare-quick"]["public"] is True
    # Only the Quick Tunnel hands out a different address on every restart, and
    # that difference is the whole point of the labelling.
    assert capabilities["cloudflare-quick"]["stable_hostname"] is False
    assert capabilities["tailscale-serve"]["stable_hostname"] is True


def test_a_daemon_backed_provider_has_no_process_to_supervise():
    assert transport.provider_capabilities("tailscale-serve").long_lived_process is False
    assert transport.provider_capabilities("manual").long_lived_process is False
    assert transport.provider_capabilities("cloudflare-quick").long_lived_process is True


def test_selecting_a_provider_persists_it_across_a_restart(tmp_path):
    # Arrange / Act
    transport.write_selection(tmp_path, "tailscale-serve")

    # Assert: a new process reading the same folder makes the same choice.
    assert transport.read_selection(tmp_path).provider == "tailscale-serve"


def test_provider_settings_survive_with_the_selection(tmp_path):
    transport.write_selection(tmp_path, "cloudflare-named", {"hostname": "music.example.com"})
    selection = transport.read_selection(tmp_path)
    assert selection.provider == "cloudflare-named"
    assert selection.config["hostname"] == "music.example.com"


def test_an_unknown_provider_is_refused_at_the_point_of_choosing(tmp_path):
    # Rejecting here rather than at launch keeps the stored value trustworthy.
    with pytest.raises(ValueError):
        transport.write_selection(tmp_path, "carrier-pigeon")


def test_a_damaged_selection_file_falls_back_to_the_default(tmp_path):
    # Arrange
    (tmp_path / transport.TRANSPORT_CONFIG_FILENAME).write_text("{not json", encoding="utf-8")

    # Act / Assert: a corrupt optional file must never change which transport
    # is in use, and never stop Rainette from starting.
    assert transport.read_selection(tmp_path).provider == "cloudflare-quick"


def test_the_manager_switches_provider_and_remembers_it(tmp_path):
    # Arrange
    manager = tunnel.TunnelManager(tmp_path, binary_locator=lambda: Path("cloudflared"))

    # Act
    status = manager.set_provider("tailscale-serve")

    # Assert
    assert status["provider"] == "tailscale-serve"
    assert status["provider_label"] == "Private link"
    assert status["stable_hostname"] is True
    assert transport.read_selection(tmp_path).provider == "tailscale-serve"


# ── migrating an install that predates the seam ───────────────────────────


def test_an_old_install_on_a_generated_address_migrates_to_the_quick_tunnel():
    # A *.trycloudflare.com address can only have come from a Quick Tunnel.
    assert transport.migrate_provider_id(
        {}, legacy_public_url="https://calm-frog-mixes.trycloudflare.com"
    ) == "cloudflare-quick"


def test_an_old_install_on_its_own_address_migrates_to_manual():
    # Anything else in that field was typed by a person, and "bring your own
    # address" is exactly what they were doing.
    assert transport.migrate_provider_id(
        {}, legacy_public_url="https://music-pc.example.com"
    ) == "manual"


def test_an_old_install_with_no_address_migrates_to_the_default():
    assert transport.migrate_provider_id({}, legacy_public_url="") == "cloudflare-quick"


def test_a_stored_provider_wins_over_the_migration_guess():
    assert transport.migrate_provider_id(
        {"provider": "tailscale-funnel"},
        legacy_public_url="https://calm-frog-mixes.trycloudflare.com",
    ) == "tailscale-funnel"


def test_a_provider_written_by_a_newer_build_falls_back_rather_than_failing():
    # Failing closed here would leave the install with no transport at all.
    assert transport.migrate_provider_id({"provider": "quantum-relay"}) == "cloudflare-quick"


def test_the_manager_migrates_from_a_legacy_address(tmp_path):
    # Arrange: no selection file, but a public address an older build wrote.
    manager = tunnel.TunnelManager(
        tmp_path,
        binary_locator=lambda: Path("cloudflared"),
        legacy_public_url=lambda: "https://music-pc.example.com",
    )

    # Act / Assert
    assert manager.status()["provider"] == "manual"


# ── which addresses Rainette may clear ────────────────────────────────────


def test_a_generated_address_is_managed_whatever_is_selected():
    # A Quick Tunnel address always dies with the process, so leaving it baked
    # into pairing links is the exact failure this area exists to prevent.
    for provider in transport.PROVIDER_IDS:
        assert transport.is_managed_url(
            "https://calm-frog-mixes.trycloudflare.com", provider=provider
        ) is True


def test_a_users_own_address_is_never_cleared_for_them():
    assert transport.is_managed_url("https://music-pc.example.com", provider="manual") is False
    assert transport.is_managed_url("", provider="manual") is False


def test_a_tailnet_address_is_managed_only_while_tailscale_is_selected():
    assert transport.is_managed_url("https://mac-mini.tail1a2b.ts.net", provider="tailscale-serve") is True
    # The same address typed in by hand under "bring your own address" is the
    # user's, and must survive a stop.
    assert transport.is_managed_url("https://mac-mini.tail1a2b.ts.net", provider="manual") is False


def test_a_named_tunnel_owns_only_its_configured_hostname():
    config = {"hostname": "music.example.com", "tunnel_name": "music"}
    assert transport.is_managed_url("https://music.example.com", provider="cloudflare-named", config=config) is True
    assert transport.is_managed_url("https://other.example.com", provider="cloudflare-named", config=config) is False


# ── preflight: the human steps, reported as steps ─────────────────────────


def test_the_quick_tunnel_never_asks_for_setup():
    """The default has to stay the option that demands nothing of the user."""
    result = transport.build_provider("cloudflare-quick").preflight()
    assert result.ok is True
    assert result.action == ""


def test_a_missing_tailscale_is_an_install_step_not_a_failure(tmp_path):
    # Arrange
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: None)

    # Act
    result = provider.preflight()

    # Assert: actionable, with somewhere to go.
    assert result.ok is False
    assert result.action == "install"
    assert result.url.startswith("https://")
    assert "Tailscale" in result.message


def test_a_logged_out_tailnet_is_a_login_step(monkeypatch, tmp_path):
    # Arrange
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(
        provider, "_status_json",
        lambda: {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/abc123"},
    )

    # Act
    result = provider.preflight()

    # Assert: the URL the daemon itself printed, so the button goes to the
    # right place rather than to a generic help page.
    assert (result.ok, result.action) == (False, "login")
    assert result.url == "https://login.tailscale.com/a/abc123"


def test_a_tailnet_without_https_certificates_is_a_consent_step(monkeypatch):
    # Arrange: signed in and named, but the tailnet has HTTPS switched off, so
    # there is no certificate and the phone could never connect.
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_status_json", lambda: {
        "BackendState": "Running",
        "Self": {"DNSName": "mac-mini.tail1a2b.ts.net."},
        "CertDomains": [],
    })

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action) == (False, "consent")
    assert "admin/dns" in result.url


def test_a_ready_tailnet_reports_the_address_the_phone_will_use(monkeypatch):
    # Arrange
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_status_json", lambda: {
        "BackendState": "Running",
        "Self": {"DNSName": "mac-mini.tail1a2b.ts.net."},
        "CertDomains": ["mac-mini.tail1a2b.ts.net"],
    })

    # Act
    result = provider.preflight()

    # Assert
    assert result.ok is True
    assert "https://mac-mini.tail1a2b.ts.net" in result.message


def test_funnels_first_run_consent_is_a_button_not_an_outage(monkeypatch):
    """The single most likely first-run stumble must not read as a crash."""
    # Arrange: Funnel on a fresh tailnet exits non-zero with a consent link.
    consent = "https://login.tailscale.com/f/funnel?node=abc123"
    provider = transport.build_provider("tailscale-funnel", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "preflight", lambda: transport.PreflightResult(ok=True))
    monkeypatch.setattr(provider, "_run", lambda command, timeout: subprocess.CompletedProcess(
        command, 1, stdout="", stderr=f"Funnel is not enabled.\nvisit {consent}\n",
    ))

    # Act / Assert
    with pytest.raises(transport.SetupRequired) as raised:
        provider.launch(47878)
    assert raised.value.result.action == "consent"
    assert raised.value.result.url == consent


def test_funnel_targets_the_pinned_companion_port_directly(monkeypatch):
    """443 is a restriction on the *public* listener, not on the local target.

    Rewriting the companion port to satisfy a limit that does not apply to it
    would break the pinned endpoint for no reason.
    """
    # Arrange
    seen: list[list[str]] = []
    provider = transport.build_provider("tailscale-funnel", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "preflight", lambda: transport.PreflightResult(ok=True))

    def record(command, timeout):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", record)

    # Act
    handle = provider.launch(47878)

    # Assert
    assert seen[0][1:] == ["funnel", "--bg", "--https=443", "http://127.0.0.1:47878"]
    assert handle.process is None  # the config lives in the daemon
    assert handle.state["port"] == 47878


def test_turning_a_daemon_provider_off_rewrites_the_daemon(monkeypatch):
    # Arrange
    seen: list[list[str]] = []
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_run", lambda command, timeout: seen.append(command) or
                        subprocess.CompletedProcess(command, 0, stdout="", stderr=""))

    # Act
    provider.stop(transport.ProviderHandle())

    # Assert: there is no process to kill, so this is the only thing that stops
    # the computer being published.
    assert seen[0][1:] == ["serve", "--https=443", "off"]


def test_a_manual_address_must_be_https():
    # An HTTPS page cannot call a plain-HTTP endpoint on another device.
    provider = transport.build_provider("manual", config={"public_url": "http://192.168.1.20:47878"})
    result = provider.preflight()
    assert (result.ok, result.action) == (False, "configure")


def test_a_manual_address_that_is_missing_asks_for_one():
    result = transport.build_provider("manual", config={}).preflight()
    assert (result.ok, result.action) == (False, "configure")


def test_a_manual_https_address_is_accepted_and_returned():
    provider = transport.build_provider("manual", config={"public_url": "https://music-pc.example.com"})
    assert provider.preflight().ok is True
    assert provider.discover_url(transport.ProviderHandle(), 0) == "https://music-pc.example.com"


def test_a_named_tunnel_without_settings_asks_for_them(monkeypatch, tmp_path):
    # Arrange: signed in to Cloudflare, but nothing configured yet.
    provider = transport.build_provider(
        "cloudflare-named", locate_cloudflared=lambda: Path("cloudflared"), config={}
    )
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".cloudflared").mkdir()
    (tmp_path / ".cloudflared" / "cert.pem").write_text("x", encoding="utf-8")

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action) == (False, "configure")


def test_a_named_tunnel_uses_its_configured_hostname_rather_than_scraping():
    provider = transport.build_provider(
        "cloudflare-named",
        locate_cloudflared=lambda: Path("cloudflared"),
        config={"hostname": "music.example.com", "tunnel_name": "music"},
    )
    assert provider.discover_url(transport.ProviderHandle(), 0) == "https://music.example.com"


# ── the manager, driving a provider that needs setup ──────────────────────


def test_starting_a_provider_that_needs_setup_reports_a_checklist(tmp_path, monkeypatch):
    """An unfinished human step is a checklist, and never an error state."""
    # Arrange
    manager = tunnel.TunnelManager(tmp_path, provider="tailscale-serve")
    monkeypatch.setattr(
        manager.provider(), "preflight",
        lambda: transport.PreflightResult(
            ok=False, action="install", message="Install Tailscale on this computer, then come back.",
            url="https://tailscale.com/download",
        ),
    )

    # Act
    status = manager.start(47878)

    # Assert
    assert status["phase"] == "setup"
    assert status["needs_setup"] is True
    assert status["setup_action"] == "install"
    assert status["setup_url"] == "https://tailscale.com/download"
    # Crucially not an error: nothing is broken, something is unfinished.
    assert status["running"] is False
    assert status["busy"] is False


def test_a_daemon_backed_provider_comes_up_with_no_process_at_all(tmp_path, monkeypatch, fast_tunnel):
    # Arrange
    manager = tunnel.TunnelManager(
        tmp_path, provider="tailscale-serve", reachable_probe=lambda url: True
    )
    provider = manager.provider()
    monkeypatch.setattr(provider, "preflight", lambda: transport.PreflightResult(ok=True))
    monkeypatch.setattr(provider, "launch", lambda port: transport.ProviderHandle(state={"port": port}))
    monkeypatch.setattr(
        provider, "discover_url", lambda handle, deadline: "https://mac-mini.tail1a2b.ts.net"
    )

    # Act
    manager.start(47878)
    status = settle(manager)

    # Assert
    assert status["phase"] == "running"
    assert status["url"] == "https://mac-mini.tail1a2b.ts.net"
    assert status["stable_hostname"] is True
    manager.stop()


def test_switching_provider_stops_what_is_already_running(tmp_path, monkeypatch, fast_tunnel):
    # Arrange: a live Quick Tunnel.
    process = FakeProcess(QUICK_TUNNEL_LOG)
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *args, **kwargs: process)
    manager = tunnel.TunnelManager(
        tmp_path,
        binary_locator=lambda: Path("cloudflared"),
        reachable_probe=lambda url: True,
    )
    manager.start(47878)
    assert settle(manager)["phase"] == "running"

    # Act
    status = manager.set_provider("manual", {"public_url": "https://music-pc.example.com"})

    # Assert: the old transport is torn down through its own provider before
    # the selection changes, or nothing would know how to turn it off.
    assert process.terminated is True
    assert status["provider"] == "manual"
    assert status["phase"] == "stopped"


def test_a_hung_but_alive_helper_is_noticed_and_replaced(tmp_path, monkeypatch, fast_tunnel):
    """`poll()` stays None forever for a helper that stopped carrying traffic.

    That was invisible to the old supervisor, and the phone was left to time
    out with nothing on the desktop to explain it.
    """
    # Arrange: reachable on the way up, then never again.
    monkeypatch.setattr(tunnel, "_HEALTH_PROBE_INTERVAL_S", 0.05)
    monkeypatch.setattr(tunnel, "_HEALTH_PROBE_FAILURES", 2)
    monkeypatch.setattr(tunnel, "_SUPERVISOR_POLL_S", 0.02)
    monkeypatch.setattr(tunnel, "_RESTART_BACKOFF_S", 0.05)

    started: list[FakeProcess] = []

    def spawn(*args, **kwargs):
        # Every helper reports the hostname and then stays alive forever, which
        # is exactly what a hung cloudflared looks like from `poll()`.
        process = FakeProcess(list(QUICK_TUNNEL_LOG))
        started.append(process)
        return process

    monkeypatch.setattr(tunnel.subprocess, "Popen", spawn)

    probes: list[str] = []

    def probe(url: str) -> bool:
        probes.append(url)
        # Answers during the first launch, never again, then answers once the
        # replacement helper is up.
        return len(probes) == 1 or len(started) > 1

    manager = tunnel.TunnelManager(
        tmp_path, binary_locator=lambda: Path("cloudflared"), reachable_probe=probe
    )
    manager.start(47878)
    assert settle(manager)["phase"] == "running"
    assert len(started) == 1

    # Act: wait for the health probes to fail often enough to count.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(started) < 2:
        time.sleep(0.02)

    # Assert: the first helper was still "alive" by `poll()` and was replaced
    # anyway, which is the whole point.
    assert len(started) == 2, f"the hung helper was never replaced (probes: {len(probes)})"
    assert started[0].terminated is True, "the hung helper was left running"
    assert settle(manager)["phase"] == "running"
    manager.stop()


def test_a_stable_address_is_not_republished_on_every_restart(tmp_path, monkeypatch, fast_tunnel):
    """Republishing an unchanged address rewrites config and flashes the UI."""
    # Arrange
    monkeypatch.setattr(tunnel, "_HEALTH_PROBE_INTERVAL_S", 0.05)
    monkeypatch.setattr(tunnel, "_HEALTH_PROBE_FAILURES", 1)
    monkeypatch.setattr(tunnel, "_SUPERVISOR_POLL_S", 0.02)
    monkeypatch.setattr(tunnel, "_RESTART_BACKOFF_S", 0.02)

    published: list[str] = []
    reachable = [True]
    manager = tunnel.TunnelManager(
        tmp_path,
        provider="tailscale-serve",
        on_url=published.append,
        reachable_probe=lambda url: reachable[0],
    )
    provider = manager.provider()
    monkeypatch.setattr(provider, "preflight", lambda: transport.PreflightResult(ok=True))
    monkeypatch.setattr(provider, "launch", lambda port: transport.ProviderHandle(state={"port": port}))
    monkeypatch.setattr(provider, "discover_url", lambda handle, deadline: "https://mac-mini.tail1a2b.ts.net")

    manager.start(47878)
    assert settle(manager)["phase"] == "running"
    assert published == ["https://mac-mini.tail1a2b.ts.net"]

    # Act: make the supervisor decide it is down, then let it come back on the
    # very same hostname.
    reachable[0] = False
    time.sleep(0.3)
    reachable[0] = True
    time.sleep(0.4)

    # Assert: the address never changed, so it was published exactly once.
    assert published == ["https://mac-mini.tail1a2b.ts.net"]
    manager.stop()


# ── the helper download stays a Cloudflare-only step ──────────────────────


def test_a_provider_that_needs_no_download_does_not_gate_on_one(tmp_path):
    # Arrange
    manager = tunnel.TunnelManager(tmp_path, provider="tailscale-serve", binary_locator=lambda: None)

    # Act
    helper = manager.helper_status()

    # Assert: "ready" so the button is not blocked, "not required" so the
    # panel can hide a download that would do nothing.
    assert helper["required"] is False
    assert helper["ready"] is True


def test_the_quick_tunnel_still_requires_its_helper(tmp_path):
    manager = tunnel.TunnelManager(tmp_path, binary_locator=lambda: None)
    helper = manager.helper_status()
    assert helper["required"] is True
    assert helper["ready"] is False
    with pytest.raises(tunnel.TunnelError):
        manager.start(47878)


# ── the config file ───────────────────────────────────────────────────────


def test_tailscales_windows_path_is_recognised_as_absolute_without_a_slash(tmp_path):
    """The concrete regression `_locate_tailscale` used to have.

    Routing was decided with ``os.sep not in candidate``, which only
    recognises the *current* platform's own separator. Every non-Windows
    candidate in ``_TAILSCALE_PATHS`` is "/"-separated and the Windows one is
    "\\"-separated, so on an actual Windows machine ``os.sep`` is ``"\\"``
    and every "/"-separated candidate would (mis)classify as a bare command
    name for ``shutil.which()`` instead of a literal path to check -- and,
    symmetrically, on POSIX the "\\"-separated Windows candidate does too.
    ``os.path.isabs`` is what the check actually means on either platform, and
    is asserted directly here (via ``ntpath``) rather than by running on
    Windows.
    """
    import ntpath
    import posixpath

    windows_candidate = next(c for c in transport._TAILSCALE_PATHS if "Program Files" in c)
    assert "/" not in windows_candidate
    assert ntpath.isabs(windows_candidate)

    for candidate in transport._TAILSCALE_PATHS:
        if candidate.startswith("/"):
            assert "\\" not in candidate
            assert posixpath.isabs(candidate)


def test_an_absolute_tailscale_candidate_is_checked_as_a_literal_file(monkeypatch, tmp_path):
    """The routing choice itself: an absolute candidate must be checked with
    ``Path.is_file()`` and must never be handed to ``shutil.which()``, whose
    PATH search is a different (and here, wrong) operation entirely.
    """
    fake_binary = tmp_path / "tailscale-here"
    fake_binary.write_text("")
    monkeypatch.setattr(transport, "_TAILSCALE_PATHS", (str(fake_binary),))
    monkeypatch.setattr(
        transport.shutil, "which",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not search PATH")),
    )

    assert transport._locate_tailscale() == fake_binary


def test_a_bare_tailscale_candidate_is_searched_on_path(monkeypatch, tmp_path):
    """The other half of the same routing choice: a bare command name must go
    through ``shutil.which()`` rather than being checked as a literal file
    relative to the current directory.
    """
    calls = []
    found = tmp_path / "found-tailscale"
    monkeypatch.setattr(transport, "_TAILSCALE_PATHS", ("tailscale-bin-name",))
    monkeypatch.setattr(transport.shutil, "which", lambda cmd: calls.append(cmd) or str(found))

    assert transport._locate_tailscale() == found
    assert calls == ["tailscale-bin-name"]


def test_no_window_kwargs_is_a_no_op_off_windows():
    assert transport._no_window_kwargs() == {}


def test_no_window_kwargs_hides_the_console_and_suppresses_a_new_one(monkeypatch):
    """A console window flashing on every launch is a real, visible bug --
    this pins the two pieces that prevent it: ``STARTF_USESHOWWINDOW`` (which
    tells Windows to honour ``wShowWindow`` at all) actually set on the
    ``STARTUPINFO`` handed back, and ``creationflags`` carrying
    ``CREATE_NO_WINDOW`` (which stops a console from being allocated in the
    first place, rather than merely hiding one after the fact).

    None of ``subprocess.STARTUPINFO``, ``CREATE_NO_WINDOW`` or
    ``STARTF_USESHOWWINDOW`` exist outside Windows, so this fakes them in
    (``create=True``) the same way the rest of this suite fakes Windows-only
    filesystem behaviour to verify it without a Windows machine.
    """
    created = []

    class FakeSTARTUPINFO:
        def __init__(self):
            self.dwFlags = 0
            created.append(self)

    monkeypatch.setattr(transport.os, "name", "nt")
    monkeypatch.setattr(transport.subprocess, "STARTUPINFO", FakeSTARTUPINFO, raising=False)
    monkeypatch.setattr(transport.subprocess, "STARTF_USESHOWWINDOW", 0x00000001, raising=False)
    monkeypatch.setattr(transport.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    kwargs = transport._no_window_kwargs()

    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"] is created[0]
    assert kwargs["startupinfo"].dwFlags & 0x00000001


def test_popen_and_run_both_forward_the_no_window_kwargs(monkeypatch):
    """The helper is useless if the two places that actually launch a process
    do not call it. ``_popen`` backs the long-lived cloudflared process;
    ``_run`` backs every one-shot Tailscale command.
    """
    sentinel = {"marker": "no-console"}
    monkeypatch.setattr(transport, "_no_window_kwargs", lambda: dict(sentinel))

    run_calls = []
    monkeypatch.setattr(
        transport.subprocess, "run",
        lambda *a, **kw: run_calls.append(kw) or subprocess.CompletedProcess(a, 0, stdout="{}", stderr=""),
    )
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    provider._run(["tailscale", "status", "--json"], timeout=5)
    assert run_calls[0]["marker"] == "no-console"

    popen_calls = []

    class FakePopen:
        def __init__(self, *a, **kw):
            popen_calls.append(kw)
            self.stdout = iter(())

    monkeypatch.setattr(transport.subprocess, "Popen", FakePopen)
    cloudflare = transport.build_provider("cloudflare-quick", locate_cloudflared=lambda: Path("cloudflared"))
    cloudflare._popen(["cloudflared", "tunnel"])
    assert popen_calls[0]["marker"] == "no-console"


def test_run_passes_an_explicit_utf8_encoding(monkeypatch):
    """``text=True`` without an explicit ``encoding=`` decodes helper output
    with ``locale.getpreferredencoding(False)``, which on Windows is commonly
    a legacy code page rather than UTF-8. A status line with non-ASCII bytes
    -- a device name, a tailnet name -- would then raise
    ``UnicodeDecodeError``, which is neither ``OSError`` nor
    ``SubprocessError`` and so is not one of the exceptions callers already
    catch. ``_popen`` already pins utf-8/replace for the long-lived helper
    process; ``_run`` must match.
    """
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))

    provider._run(["tailscale", "status", "--json"], timeout=5)

    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


def test_run_actually_decodes_non_ascii_helper_output(tmp_path):
    """Not just the kwargs: bytes that are valid UTF-8 but invalid in common
    single-byte Windows code pages must still come back as the right text.
    """
    text = "café \u65e5\u672c\u8a9e"  # accented and non-Latin bytes together
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    script = tmp_path / "echo_utf8.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({text!r}.encode('utf-8'))\n"
    )

    completed = provider._run([sys.executable, str(script)], timeout=10)

    assert completed.stdout == text


def test_the_selection_file_is_valid_json(tmp_path):
    transport.write_selection(tmp_path, "cloudflare-named", {"hostname": "music.example.com"})
    payload = json.loads((tmp_path / transport.TRANSPORT_CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert payload == {
        "provider": "cloudflare-named",
        "provider_config": {"hostname": "music.example.com"},
    }
