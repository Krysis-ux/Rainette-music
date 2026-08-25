"""Tests for the transport provider seam.

The property under test throughout is that adding options did not remove the
one that already worked.  A user who never opens the new picker must get the
Cloudflare Quick Tunnel, on a fresh install and on an install that predates the
seam entirely, and every new provider must report unfinished setup as a
checklist rather than as a failure.
"""

from __future__ import annotations

import base64
import json
import os
import re
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
    assert status["provider_label"] == "Works anywhere"
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
    """Labels describe what the user gets, not which vendor supplies it."""
    labels = {entry["id"]: entry["label"] for entry in transport.catalogue()}
    assert labels["cloudflare-quick"] == "Works anywhere"
    assert labels["tailscale-serve"] == "Direct on your network"
    assert labels["tailscale-funnel"] == "Direct, plus reachable anywhere"
    # The recommendation is the one that is both fast on a home network and
    # still a secure context, so the phone client stays installable.
    # The recommendation follows the default. Tailscale is better where it
    # applies, but it asks for a VPN app on both devices; the plain tunnel needs
    # nothing and is what most people will use, so it is the one recommended --
    # and the one that has to work.
    recommended = [entry["id"] for entry in transport.catalogue() if entry["recommended"]]
    assert recommended == [transport.DEFAULT_PROVIDER]
    assert transport.DEFAULT_PROVIDER == "cloudflare-quick"


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
    assert status["provider_label"] == "Direct on your network"
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
    # A pending auth URL is the user's step to finish, so Rainette does not
    # offer to redo it underneath them.
    assert result.can_fix is False


def test_a_tailnet_without_https_names_the_switch_and_says_it_is_free(monkeypatch):
    """This is the step that reads as "it cannot connect" when it is skipped.

    Without it `tailscale cert` answers "your Tailscale account does not support
    getting TLS certs", so the phone has nothing to trust. Pointing at a
    settings page is not enough — the message has to name the switch.
    """
    # Arrange: connected, named, but HTTPS is off for the tailnet.
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_status_json", lambda: {
        "BackendState": "Running",
        "Self": {"DNSName": "box.tailnet.ts.net."},
        "CertDomains": [],
    })

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action) == (False, "consent")
    assert "HTTPS Certificates" in result.message
    assert "free" in result.detail
    assert result.url == "https://login.tailscale.com/admin/dns"
    # Not something Rainette can flip on somebody's account.
    assert result.can_fix is False


def test_the_start_button_reports_the_same_step_as_the_panel_does(tmp_path, monkeypatch):
    """`start` and `preflight` must not describe the same state differently.

    The detail line and the fix button were dropped on the start path, so
    pressing "turn this connection on" produced a bare sentence while asking
    the same question through the panel produced a full step.
    """
    # Arrange
    manager = tunnel.TunnelManager(tmp_path, provider="tailscale-serve")
    provider = manager.provider()
    # Without this the test asks the *host* whether Tailscale is installed, so
    # it reported "install" on a clean machine and only passed where a developer
    # happened to have it. The question here is whether start and preflight
    # agree, which has nothing to do with either.
    monkeypatch.setattr(provider, "ensure_binary", lambda *_a, **_k: Path("tailscale"))
    monkeypatch.setattr(provider, "_status_json", lambda: {
        "BackendState": "Running",
        "Self": {"DNSName": "box.tailnet.ts.net."},
        "CertDomains": [],
    })

    # Act
    status = manager.start(8765)
    status = settle(manager)

    # Assert
    assert status["phase"] == "setup"
    assert status["setup_action"] == "consent"
    assert status["setup_detail"], "the start path dropped the explanation"
    assert status["setup_detail"] == provider.preflight().detail


def test_a_sleeping_tailscale_offers_to_start_it_rather_than_blaming_the_user(monkeypatch):
    """`tailscale up` cannot help when the daemon it talks to is not running.

    On macOS the CLI is a client of a daemon the app owns, so with the app quit
    the old panel said "open the Tailscale app once, then try again" beside a
    button that was guaranteed to fail. Rainette can just open it.
    """
    # Arrange: installed, daemon not answering.
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_status_json", lambda: None)

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action, result.can_fix) == (False, "login", True)
    assert result.fix_label == "Start Tailscale"
    assert "try again" not in result.message.lower()


def test_starting_a_sleeping_tailscale_opens_the_app_before_running_up(monkeypatch):
    """Order matters: `up` against a dead daemon fails for the reason just fixed."""
    # Arrange
    calls = []
    answers = [None, None, {"BackendState": "Running"}]

    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(transport.sys, "platform", "darwin")
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(provider, "_status_json", lambda: answers.pop(0) if answers else {"BackendState": "Running"})

    def fake_subprocess_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fake_run(command, *, timeout):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(transport.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(provider, "_run", fake_run)

    # Act
    provider.begin_login()
    thread = provider._login_thread
    if thread is not None:
        thread.join(timeout=5)

    # Assert
    assert calls[0][:2] == ["/usr/bin/open", "-a"]
    assert calls[-1][1:] == ["up"]


def test_a_responsive_tailscale_is_not_relaunched(monkeypatch):
    """Opening an app that is already running would steal focus for nothing."""
    # Arrange
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(transport.sys, "platform", "darwin")
    monkeypatch.setattr(provider, "_status_json", lambda: {"BackendState": "NeedsLogin"})
    opened = []
    monkeypatch.setattr(transport.subprocess, "run",
                        lambda command, **kwargs: opened.append(command) or subprocess.CompletedProcess(command, 0))

    # Act
    provider._wake_daemon()

    # Assert
    assert opened == []


def test_a_logged_out_tailnet_with_no_auth_url_offers_to_start_the_login(monkeypatch):
    """Logged out, the daemon has no AuthURL until something runs `tailscale up`.

    That is the whole reason this step is a button: the old flow linked to a
    login page that could not know which computer was asking.
    """
    # Arrange
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_status_json", lambda: {"BackendState": "NeedsLogin"})

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action, result.can_fix) == (False, "login", True)
    assert result.fix_label == "Connect Tailscale"


def test_starting_the_tailscale_login_runs_up_rather_than_opening_a_page(monkeypatch):
    """`tailscale up` is what makes the daemon mint the link, so it is the step."""
    # Arrange
    commands = []

    def fake_run(command, *, timeout):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(provider, "_run", fake_run)

    # Act
    result = provider.begin_login()
    thread = provider._login_thread
    if thread is not None:
        thread.join(timeout=5)

    # Assert: `up` is reached. It is not necessarily the first call — waking a
    # sleeping daemon interrogates it first — so the check is that it happened.
    assert result["ok"] is True
    assert ["up"] in [command[1:] for command in commands]


def test_a_printed_auth_url_is_not_reported_as_a_tailscale_failure(monkeypatch):
    """`tailscale up` exiting non-zero while printing a link is the normal path."""
    # Arrange
    provider = transport.build_provider("tailscale-serve", locate_tailscale=lambda: Path("tailscale"))
    monkeypatch.setattr(
        provider,
        "_run",
        lambda command, *, timeout: subprocess.CompletedProcess(
            command, 1, stdout="To authenticate, visit:\n\nhttps://login.tailscale.com/a/abc123\n", stderr=""
        ),
    )

    # Act
    provider.begin_login()
    thread = provider._login_thread
    if thread is not None:
        thread.join(timeout=5)

    # Assert
    assert provider._login_error == ""


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


def _named_provider(tmp_path, monkeypatch, *, config=None, run=None):
    """A named-tunnel provider whose home directory and CLI are under test control."""
    provider = transport.build_provider(
        "cloudflare-named",
        locate_cloudflared=lambda: Path("cloudflared"),
        config=config or {},
    )
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".cloudflared").mkdir(exist_ok=True)
    if run is not None:
        monkeypatch.setattr(provider, "_run", run)
    return provider


def _sign_in(tmp_path):
    (tmp_path / ".cloudflared" / "cert.pem").write_text("x", encoding="utf-8")


def test_a_named_tunnel_without_settings_offers_to_set_itself_up(monkeypatch, tmp_path):
    """The step after signing in is a button Rainette presses, not a form."""
    # Arrange: signed in to Cloudflare, but nothing configured yet.
    provider = _named_provider(tmp_path, monkeypatch)
    _sign_in(tmp_path)
    monkeypatch.setattr(provider, "zone_name", lambda: "example.com")

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action) == (False, "provision")
    assert result.can_fix is True
    assert "music.example.com" in result.message


def test_a_named_tunnel_that_cannot_read_the_domain_still_asks_rather_than_fails(
    monkeypatch, tmp_path
):
    """Losing the zone lookup costs a prefilled box, never the feature."""
    # Arrange
    provider = _named_provider(tmp_path, monkeypatch)
    _sign_in(tmp_path)
    monkeypatch.setattr(provider, "zone_name", lambda: "")

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action) == (False, "provision")
    assert result.can_fix is False


def test_signing_in_is_a_button_rather_than_a_terminal_command(monkeypatch, tmp_path):
    """The old flow told people to run a command; this one offers to do it."""
    # Arrange: helper present, not signed in.
    provider = _named_provider(tmp_path, monkeypatch)

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action) == (False, "login")
    assert result.can_fix is True
    assert "terminal" not in result.message.lower()
    # Somebody with no account at all needs the signup page offered too.
    assert result.url == "https://dash.cloudflare.com/sign-up"


def test_the_missing_helper_is_a_download_rainette_can_do(monkeypatch, tmp_path):
    # Arrange: no cloudflared anywhere.
    provider = transport.build_provider("cloudflare-named", locate_cloudflared=lambda: None)
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: tmp_path))

    # Act
    result = provider.preflight()

    # Assert
    assert (result.ok, result.action, result.can_fix) == (False, "install", True)


def test_provisioning_creates_the_tunnel_and_points_the_name_at_it(monkeypatch, tmp_path):
    """One button has to cover both CLI calls, or it is not one button."""
    # Arrange
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    provider = _named_provider(tmp_path, monkeypatch, run=fake_run)
    _sign_in(tmp_path)

    # Act
    result = provider.provision(hostname="music.example.com", tunnel_name="rainette-mac")

    # Assert
    assert result == {"ok": True, "tunnel_name": "rainette-mac", "hostname": "music.example.com"}
    assert calls[0][1:] == ["tunnel", "create", "rainette-mac"]
    assert calls[1][1:] == ["tunnel", "route", "dns", "rainette-mac", "music.example.com"]
    # And the provider is now ready without anybody typing anything.
    assert provider.preflight().ok is True


def test_provisioning_again_lands_on_ready_rather_than_already_exists(monkeypatch, tmp_path):
    """Pressing the button twice is the normal case, not an error."""
    # Arrange
    def fake_run(command, *, timeout):
        if "create" in command:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="tunnel with name rainette-mac already exists"
            )
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="already configured to point to tunnel rainette-mac"
        )

    provider = _named_provider(tmp_path, monkeypatch, run=fake_run)
    _sign_in(tmp_path)

    # Act
    result = provider.provision(hostname="music.example.com", tunnel_name="rainette-mac")

    # Assert
    assert result["ok"] is True


def test_a_dns_record_owned_by_something_else_is_a_real_failure(monkeypatch, tmp_path):
    """Silently stealing a record that serves somebody's website would be worse."""
    # Arrange
    def fake_run(command, *, timeout):
        if "create" in command:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="An A, AAAA, or CNAME record with that host already exists"
        )

    provider = _named_provider(tmp_path, monkeypatch, run=fake_run)
    _sign_in(tmp_path)

    # Act / Assert
    with pytest.raises(transport.TransportError):
        provider.provision(hostname="www.example.com", tunnel_name="rainette-mac")


def test_the_suggested_tunnel_name_is_safe_for_cloudflare(monkeypatch):
    """Hostnames can carry anything; a tunnel name cannot."""
    # Arrange
    monkeypatch.setattr(transport.socket, "gethostname", lambda: "Lennon's MacBook Pro.local")

    # Act
    name = transport.CloudflareNamedProvider.suggested_tunnel_name()

    # Assert
    assert name == "rainette-lennon-s-macbook-pro"
    assert re.fullmatch(r"[a-z0-9-]+", name)


def test_the_zone_is_read_from_the_certificate_cloudflared_wrote(monkeypatch, tmp_path):
    """Reading the chosen domain locally is what removes the text box."""
    # Arrange
    provider = _named_provider(tmp_path, monkeypatch)
    token = base64.b64encode(
        json.dumps({"zoneID": "zone-1", "apiToken": "secret"}).encode()
    ).decode()
    (tmp_path / ".cloudflared" / "cert.pem").write_text(
        "-----BEGIN CERTIFICATE-----\nxx\n-----END CERTIFICATE-----\n"
        f"-----BEGIN ARGO TUNNEL TOKEN-----\n{token}\n-----END ARGO TUNNEL TOKEN-----\n",
        encoding="utf-8",
    )
    seen = {}

    class FakeResponse:
        def read(self, *_):
            return json.dumps({"result": {"name": "Example.COM"}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)

    # Act
    zone = provider.zone_name()

    # Assert
    assert zone == "example.com"
    assert seen["url"].endswith("/zones/zone-1")
    assert seen["auth"] == "Bearer secret"


def test_an_unreadable_certificate_yields_no_zone_instead_of_raising(monkeypatch, tmp_path):
    # Arrange
    provider = _named_provider(tmp_path, monkeypatch)
    (tmp_path / ".cloudflared" / "cert.pem").write_text("not a token", encoding="utf-8")

    # Act / Assert
    assert provider.zone_name() == ""
    assert provider.suggested_hostname() == ""


def test_a_named_tunnel_uses_its_configured_hostname_rather_than_scraping():
    provider = transport.build_provider(
        "cloudflare-named",
        locate_cloudflared=lambda: Path("cloudflared"),
        config={"hostname": "music.example.com", "tunnel_name": "music"},
    )
    assert provider.discover_url(transport.ProviderHandle(), 0) == "https://music.example.com"


# ── the manager, carrying out a setup step on the user's behalf ───────────


def test_the_manager_refuses_a_step_the_provider_never_offered(tmp_path):
    """`setup_step` must not become a way to call arbitrary provider methods."""
    # Arrange
    manager = tunnel.TunnelManager(tmp_path, provider="cloudflare-quick")

    # Act / Assert
    with pytest.raises(tunnel.TunnelError):
        manager.setup_step("owns_url")


def test_the_manager_refuses_a_sign_in_from_a_provider_without_one(tmp_path):
    # Arrange: the quick tunnel has no account and so no begin_login.
    manager = tunnel.TunnelManager(tmp_path, provider="cloudflare-quick")

    # Act / Assert
    with pytest.raises(tunnel.TunnelError):
        manager.setup_step("login")


def test_provisioning_through_the_manager_persists_what_it_created(tmp_path, monkeypatch):
    """A tunnel created and then forgotten on restart would be worse than none."""
    # Arrange
    manager = tunnel.TunnelManager(
        tmp_path,
        provider="cloudflare-named",
        binary_locator=lambda: Path("cloudflared"),
    )
    provider = manager.provider()
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".cloudflared").mkdir(exist_ok=True)
    (tmp_path / ".cloudflared" / "cert.pem").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        provider,
        "_run",
        lambda command, *, timeout: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(provider, "zone_name", lambda: "example.com")

    # Act
    result = manager.setup_step("provision")

    # Assert
    assert result["step"] == "provision"
    assert result["step_result"]["hostname"] == "music.example.com"
    # Persisted, so a restart comes back ready rather than back at step one.
    stored = transport.read_selection(tmp_path)
    assert stored.provider == "cloudflare-named"
    assert stored.config["hostname"] == "music.example.com"


def test_a_setup_step_reports_the_next_step_rather_than_only_its_own_result(tmp_path, monkeypatch):
    """The panel redraws from preflight, so every step has to return one."""
    # Arrange: helper missing, so the step to take is the download.
    manager = tunnel.TunnelManager(tmp_path, provider="cloudflare-named", binary_locator=lambda: None)
    monkeypatch.setattr(transport.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(manager, "download_helper", lambda: {"phase": "downloading"})

    # Act
    result = manager.setup_step("install")

    # Assert
    assert result["setup_action"] == "install"
    assert result["setup_can_fix"] is True
    assert "preflight_ok" in result


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


def test_no_window_kwargs_matches_the_platform_it_runs_on():
    """Console suppression is Windows-only plumbing.

    Asserted against the real platform rather than assuming POSIX: on Windows
    returning the flags IS the correct answer, and a test that demands {}
    there would be demanding the bug.
    """
    kwargs = transport._no_window_kwargs()
    if os.name == "nt":
        assert kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
        assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    else:
        assert kwargs == {}


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
    # Written as UTF-8 explicitly: the default here is the console code page,
    # which on Windows is cp1252 and cannot represent this source at all.
    script.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({text!r}.encode('utf-8'))\n",
        encoding="utf-8",
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
