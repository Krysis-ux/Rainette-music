"""Zero-setup HTTPS tunnel for the Rainette companion gateway.

A phone can only reach this computer through an address that is both HTTPS and
routable from outside the house.  Requiring every user to research Cloudflare
Tunnel, install a CLI, and paste a hostname into Settings was the single
biggest reason pairing failed.  Without a tunnel the pairing QR carried
``http://127.0.0.1:<port>``; on a phone that address means *the phone itself*,
and an HTTPS page is not allowed to call it at all.  The browser rejects the
request before it leaves the device and reports only "Failed to fetch"
(Chromium) or "Load failed" (WebKit), which says nothing about the real cause.

This module removes the research step.  It fetches Cloudflare's ``cloudflared``
binary on demand, runs a Quick Tunnel in front of the loopback companion port,
waits until the public hostname actually answers *this* gateway, and supervises
it for as long as Rainette is running.

Trust model: the binary is downloaded over TLS from Cloudflare's own GitHub
release channel and from nowhere else — the host allowlist below is enforced on
every hop of the redirect chain, so a hijacked redirect cannot substitute a
different origin.  The download is size-capped and the result must identify
itself through ``cloudflared --version`` before it is ever used as a tunnel.

Quick Tunnels are anonymous and get a fresh ``*.trycloudflare.com`` hostname on
every start.  That is the trade for needing no Cloudflare account; the desktop
persists whichever hostname is currently live, and a phone that already holds a
device credential only has to re-scan the QR to learn the new address (its
credential still authenticates, so no re-approval is required).

*What lives where.*  This module owns the download policy above, the
reachability probe, and the supervisor — the parts that are the same no matter
how the computer became reachable.  How it becomes reachable is a
:mod:`transport` provider, and the Quick Tunnel is one of them.  It is still
the default and still behaves exactly as described above; the other providers
are extra options, never a replacement.
"""

from __future__ import annotations

import io
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import transport

# Cloudflare publishes cloudflared here for every platform Rainette runs on.
# "latest/download" always resolves to the newest release, which matters because
# Quick Tunnel support is a server-side feature that older clients lose access
# to over time.
_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# Every hop of the download must stay inside Cloudflare's GitHub release
# channel.  GitHub serves release assets by redirecting to its CDN, so the CDN
# hosts are listed too; nothing else is accepted.
_ALLOWED_DOWNLOAD_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "githubusercontent.com",
)

_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
# The hostname pattern and the "we are connected" log marker moved to the
# Cloudflare provider; kept here as names because they describe this module's
# subject matter and callers have referred to them.
_QUICK_TUNNEL_RE = transport._QUICK_TUNNEL_RE
_REGISTERED_MARKER = transport._REGISTERED_MARKER
# cloudflared prints the assigned hostname within a few seconds; the generous
# ceiling covers a cold start on a slow connection before we call it a failure.
_URL_DISCOVERY_TIMEOUT_S = 60.0
# Cloudflare's edge returns 502/530 for a few seconds after the hostname is
# printed but before the route is live, so the URL alone is not proof of reach.
_REACHABLE_TIMEOUT_S = 180.0
_REGISTRATION_TIMEOUT_S = 45.0

# The hostname cloudflared just printed does not exist in DNS for another ten
# seconds. Asking too early is not free: the resolver caches the NXDOMAIN and
# answers every later attempt from that cache, keeping the name unresolvable
# for the whole startup window. So hold off, then back off between probes.
_REACHABLE_GRACE_S = 12.0
_PROBE_INTERVAL_S = 5.0
_PROBE_INTERVAL_MAX_S = 20.0
_PROBE_BACKOFF = 1.5
_PROBE_TIMEOUT_S = 10.0
_SUPERVISOR_POLL_S = 2.0
_RESTART_BACKOFF_S = 5.0
_LOG_TAIL_LINES = 40

# A helper that is alive but no longer carrying traffic is invisible to
# `poll()` — it stays `None` forever while the phone silently times out. So the
# address itself is re-checked on a slow cadence, and only a run of failures
# counts, because one failed probe is far more likely to be the desktop's own
# network hiccupping than the tunnel being gone.
_HEALTH_PROBE_INTERVAL_S = 120.0
_HEALTH_PROBE_FAILURES = 3


class TunnelError(transport.TransportError):
    """A tunnel could not be prepared, started, or reached."""


@dataclass(frozen=True)
class TunnelStatus:
    """Immutable snapshot of the tunnel, safe to hand straight to the UI."""

    phase: str = "stopped"
    url: str = ""
    message: str = ""
    port: int = 0
    provider: str = transport.DEFAULT_PROVIDER
    provider_label: str = "Limited tunnel"
    stable_hostname: bool = False
    public: bool = True
    # Unfinished setup is not a failure. These three carry the next human step —
    # install this, sign in there, approve this once — so the settings page can
    # render a checklist with a button instead of an error nobody can act on.
    setup_action: str = ""  # "" | "install" | "login" | "consent" | "configure"
    setup_message: str = ""
    setup_url: str = ""

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "url": self.url,
            "message": self.message,
            "port": self.port,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "stable_hostname": self.stable_hostname,
            "public": self.public,
            "setup_action": self.setup_action,
            "setup_message": self.setup_message,
            "setup_url": self.setup_url,
            "running": self.phase == "running",
            "busy": self.phase == "starting",
            "needs_setup": self.phase == "setup",
        }


@dataclass(frozen=True)
class HelperStatus:
    """State of the cloudflared binary, tracked separately from the tunnel.

    Downloading is its own visible step rather than something the tunnel button
    does silently: it is the only part of this feature that reaches the network
    on the user's behalf, and it only ever has to happen once.
    """

    phase: str = "missing"
    path: str = ""
    message: str = ""
    # False for a transport that brings its own binary or none at all, so the
    # panel can hide a download button that would do nothing.
    required: bool = True

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "path": self.path,
            "message": self.message,
            "required": self.required,
            "ready": self.phase == "ready",
            "busy": self.phase == "downloading",
        }


def _tools_dir(app_data_dir: Path) -> Path:
    return Path(app_data_dir) / "tools"


def _asset_name() -> str:
    """Name of the cloudflared release asset for the running platform."""
    machine = (os.uname().machine if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "")).lower()
    arm = any(tag in machine for tag in ("arm", "aarch64"))
    if os.name == "nt":
        return "cloudflared-windows-amd64.exe" if sys.maxsize > 2**32 else "cloudflared-windows-386.exe"
    if sys.platform == "darwin":
        # Cloudflare ships macOS builds as a .tgz containing a single binary.
        return "cloudflared-darwin-arm64.tgz" if arm else "cloudflared-darwin-amd64.tgz"
    if arm:
        return "cloudflared-linux-arm64" if sys.maxsize > 2**32 else "cloudflared-linux-arm"
    return "cloudflared-linux-amd64" if sys.maxsize > 2**32 else "cloudflared-linux-386"


def _binary_name() -> str:
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def _no_window_kwargs() -> dict:
    """Keep helper processes from flashing a console window on Windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _host_allowed(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_DOWNLOAD_HOSTS)


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves Cloudflare's GitHub release channel."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_allowed(newurl):
            raise TunnelError("the cloudflared download was redirected off GitHub; refusing to continue")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_release_asset(url: str):
    if urlparse(url).scheme != "https" or not _host_allowed(url):
        raise TunnelError(f"refusing to download cloudflared from an unexpected address: {url}")
    opener = urllib.request.build_opener(
        _PinnedRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Rainette-Music"})
    return opener.open(request, timeout=60)


def _download_asset(url: str, progress: Callable[[str], None] | None = None) -> bytes:
    """Fetch a release asset into memory with a hard size ceiling."""
    buffer = io.BytesIO()
    try:
        with _open_release_asset(url) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > _MAX_DOWNLOAD_BYTES:
                raise TunnelError("the cloudflared download is unexpectedly large; refusing it")
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                buffer.write(chunk)
                if buffer.tell() > _MAX_DOWNLOAD_BYTES:
                    raise TunnelError("the cloudflared download exceeded its size limit")
                if progress is not None and declared:
                    progress(f"Downloading the secure tunnel helper… {buffer.tell() * 100 // declared}%")
    except urllib.error.URLError as exc:
        raise TunnelError(f"could not download the tunnel helper: {exc.reason}") from exc
    except OSError as exc:
        raise TunnelError(f"could not download the tunnel helper: {exc}") from exc
    if not buffer.tell():
        raise TunnelError("the tunnel helper download was empty")
    return buffer.getvalue()


def _extract_binary(payload: bytes, asset: str) -> bytes:
    """Return the raw executable, unwrapping the macOS .tgz when needed."""
    if not asset.endswith(".tgz"):
        return payload
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and Path(member.name).name == "cloudflared":
                extracted = archive.extractfile(member)
                if extracted is not None:
                    return extracted.read()
    raise TunnelError("the downloaded macOS archive did not contain cloudflared")


def _identifies_as_cloudflared(path: Path) -> bool:
    """Confirm the file we are about to run is really cloudflared."""
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "cloudflared" in f"{completed.stdout}{completed.stderr}".lower()


def locate_cloudflared(app_data_dir: Path) -> Path | None:
    """Find a usable cloudflared: a system install first, then our own copy.

    Preferring the system copy means a user who already runs a managed named
    tunnel keeps their own binary and its update cadence.
    """
    found = shutil.which("cloudflared")
    if found:
        candidate = Path(found)
        if _identifies_as_cloudflared(candidate):
            return candidate
    cached = _tools_dir(app_data_dir) / _binary_name()
    if cached.is_file() and _identifies_as_cloudflared(cached):
        return cached
    return None


def ensure_cloudflared(app_data_dir: Path, progress: Callable[[str], None] | None = None) -> Path:
    """Return a working cloudflared, downloading it once if necessary."""
    existing = locate_cloudflared(app_data_dir)
    if existing is not None:
        return existing

    if progress is not None:
        progress("Downloading the secure tunnel helper…")
    asset = _asset_name()
    payload = _extract_binary(_download_asset(f"{_RELEASE_BASE}/{asset}", progress), asset)

    tools = _tools_dir(app_data_dir)
    tools.mkdir(parents=True, exist_ok=True)
    destination = tools / _binary_name()
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        staging.write_bytes(payload)
        staging.chmod(0o755)
        os.replace(staging, destination)
    except OSError as exc:
        raise TunnelError(f"could not save the tunnel helper: {exc}") from exc
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass

    if not _identifies_as_cloudflared(destination):
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise TunnelError("the downloaded tunnel helper did not run; check your antivirus or network")
    return destination


def _probe_reachable(url: str) -> bool:
    """True once the public hostname reaches *this* companion gateway.

    ``/status`` requires a device credential, so an unauthenticated 401 from our
    own handler is the cleanest possible proof that the whole path works.
    Cloudflare's own 502/530 error pages arrive while the route is still warming
    up and are correctly treated as not-yet-reachable.
    """
    request = urllib.request.Request(
        url.rstrip("/") + "/status",
        headers={"User-Agent": "Rainette-Music-Tunnel-Probe"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_S) as response:
            return response.status in (200, 401)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 401)
    except (urllib.error.URLError, OSError, ValueError):
        return False


class TunnelManager:
    """Owns whichever transport is selected and the thread that supervises it.

    Every public method returns immediately.  Downloading and starting happen on
    a worker thread so a pywebview API call from the settings page never blocks
    the UI, and callers poll :meth:`status` instead.

    The manager knows nothing about Cloudflare or Tailscale specifically.  It
    knows how to bring *a* transport up, prove the address answers this gateway,
    and keep it that way — the provider supplies the rest.
    """

    def __init__(
        self,
        app_data_dir: Path,
        *,
        on_url: Callable[[str], None] | None = None,
        binary_locator: Callable[[], Path | None] | None = None,
        binary_installer: Callable[[Callable[[str], None] | None], Path] | None = None,
        reachable_probe: Callable[[str], bool] = _probe_reachable,
        provider: str = "",
        provider_config: dict | None = None,
        legacy_public_url: Callable[[], str] | None = None,
        provider_factory: Callable[..., transport._BaseProvider] | None = None,
    ) -> None:
        self._app_data_dir = Path(app_data_dir)
        self._on_url = on_url
        self._binary_locator = binary_locator or (lambda: locate_cloudflared(self._app_data_dir))
        self._binary_installer = binary_installer or (
            lambda progress: ensure_cloudflared(self._app_data_dir, progress)
        )
        self._reachable_probe = reachable_probe
        self._lock = threading.RLock()
        self._status = TunnelStatus()
        self._helper = HelperStatus()
        self._process: subprocess.Popen | None = None
        self._handle: transport.ProviderHandle | None = None
        self._worker: threading.Thread | None = None
        self._helper_worker: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._log: list[str] = []
        self._generation = 0
        self._stop_requested = threading.Event()
        self._probe_failures = 0
        self._next_probe_at = 0.0
        # An address written by a build that predates this seam is the only
        # evidence such an install has of which transport it was using.
        self._legacy_public_url = legacy_public_url
        self._provider_factory = provider_factory or transport.build_provider
        self._provider_id = str(provider or "")
        self._provider_config = dict(provider_config or {})
        self._provider: transport._BaseProvider | None = None

    # ── the selected transport ────────────────────────────────────────────

    def _selection(self) -> transport.TransportSelection:
        if transport.known_provider(self._provider_id):
            return transport.TransportSelection(self._provider_id, dict(self._provider_config))
        legacy = ""
        if self._legacy_public_url is not None:
            try:
                legacy = str(self._legacy_public_url() or "")
            except Exception:
                legacy = ""
        return transport.read_selection(self._app_data_dir, legacy_public_url=legacy)

    def provider(self) -> transport._BaseProvider:
        """The live provider, built once and reused until the choice changes."""
        with self._lock:
            if self._provider is not None:
                return self._provider
        selection = self._selection()
        built = self._provider_factory(
            selection.provider,
            config=selection.config,
            probe=self._reachable_probe,
            record=self._record,
            locate_cloudflared=self._binary_locator,
            install_cloudflared=self._binary_installer,
        )
        with self._lock:
            if self._provider is None:
                self._provider = built
                self._provider_id = selection.provider
                self._provider_config = dict(selection.config)
            return self._provider

    def providers(self) -> list[dict]:
        """Every transport this build can offer, in the order to show them."""
        return transport.catalogue()

    def provider_config(self) -> dict:
        """The settings the selected transport was given, for the UI to prefill."""
        self.provider()  # resolves the selection, populating the cache
        with self._lock:
            return dict(self._provider_config)

    def set_provider(self, provider_id: str, config: dict | None = None) -> dict:
        """Choose a transport and persist the choice.

        Anything currently running is torn down through its *own* provider
        first — swapping the selection out from under a live transport would
        leave a daemon still publishing this computer with nothing left that
        knows how to turn it off.
        """
        if self.status().get("running") or self.status().get("busy"):
            self.stop()
        selection = transport.write_selection(self._app_data_dir, provider_id, config)
        with self._lock:
            self._provider = None
            self._provider_id = selection.provider
            self._provider_config = dict(selection.config)
            self._status = TunnelStatus()
        return self.status()

    def preflight(self) -> dict:
        """Ask the selected transport what the user still has to do, if anything."""
        provider = self.provider()
        try:
            result = provider.preflight()
        except Exception as exc:
            result = transport.PreflightResult(ok=False, message=str(exc))
        if not result.ok:
            self._set_status(
                setup_action=result.action,
                setup_message=result.message,
                setup_url=result.url,
            )
        else:
            self._set_status(setup_action="", setup_message="", setup_url="")
        # Deliberately not the result's own ``ok``: "this needs setup" is a
        # perfectly successful answer to the question, and a caller that reads
        # ``ok`` as "the call worked" must not see it as a failure.
        return {**self.status(), "preflight_ok": result.ok}

    # ── status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        try:
            provider = self.provider()
        except Exception:
            with self._lock:
                return self._status.as_dict()
        capabilities = provider.capabilities
        with self._lock:
            self._status = replace(
                self._status,
                provider=provider.id,
                provider_label=provider.label,
                stable_hostname=capabilities.stable_hostname,
                public=capabilities.public,
            )
            return self._status.as_dict()

    def _set_status(self, **fields) -> None:
        with self._lock:
            self._status = replace(self._status, **fields)

    def _set_helper(self, **fields) -> None:
        with self._lock:
            self._helper = replace(self._helper, **fields)

    # ── the cloudflared helper ────────────────────────────────────────────

    def helper_status(self) -> dict:
        """Report whether cloudflared is available.

        A settled answer is kept as-is: re-probing a finished download would
        overwrite it, and a probe is only useful while we still believe the
        helper is absent — for instance when the user installed one themselves
        with this panel already open.  :meth:`start` re-locates the binary
        anyway, so a stale "ready" can never turn into a confusing launch.
        """
        if not self.provider().capabilities.auto_installable:
            # Nothing to download, and the tunnel button must not be gated on a
            # step that does not exist for this transport.
            return HelperStatus(
                phase="ready",
                message="This option does not need a download.",
                required=False,
            ).as_dict()

        with self._lock:
            settled = self._helper if self._helper.phase in {"downloading", "ready"} else None
        if settled is not None:
            return settled.as_dict()

        located = self._binary_locator()
        with self._lock:
            if located is not None:
                self._helper = HelperStatus(
                    phase="ready", path=str(located), message="cloudflared is ready."
                )
            elif self._helper.phase != "error":
                # Keep an explanatory failure visible instead of resetting it to
                # a bare "missing" the moment the UI polls again.
                self._helper = HelperStatus(message="cloudflared has not been downloaded yet.")
            return self._helper.as_dict()

    def download_helper(self) -> dict:
        """Fetch cloudflared on a worker thread; returns immediately."""
        if not self.provider().capabilities.auto_installable:
            return self.helper_status()
        with self._lock:
            if self._helper.phase == "downloading":
                return self._helper.as_dict()
            self._helper = HelperStatus(phase="downloading", message="Starting the download…")
            self._helper_worker = threading.Thread(
                target=self._download_helper,
                name="rainette-tunnel-helper",
                daemon=True,
            )
            self._helper_worker.start()
            return self._helper.as_dict()

    def _download_helper(self) -> None:
        try:
            path = self._binary_installer(lambda message: self._set_helper(message=message))
            self._set_helper(phase="ready", path=str(path), message="cloudflared is ready.")
        except TunnelError as exc:
            self._set_helper(phase="error", path="", message=str(exc))
        except Exception as exc:
            self._set_helper(phase="error", path="", message=f"the download failed: {exc}")

    def _record(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        with self._lock:
            self._log.append(text)
            del self._log[:-_LOG_TAIL_LINES]

    def _log_tail(self, lines: int = 3) -> str:
        with self._lock:
            return " | ".join(self._log[-lines:])

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, port: int) -> dict:
        """Begin bringing a tunnel up in front of ``port``; returns immediately.

        A transport that can install itself has to already be installed:
        downloading is a separate, named step so the one action that reaches the
        network stays something the user chose rather than a side effect of
        pressing "generate".  A transport that *cannot* install itself reports
        the missing pieces as setup instead, because "install Tailscale and sign
        in" is a checklist, not an error.
        """
        selected = int(port)
        if selected <= 0 or selected > 65535:
            raise ValueError("companion port must be between 1 and 65535")
        provider = self.provider()
        if provider.capabilities.auto_installable and provider.ensure_binary(None) is None:
            self._set_helper(message="cloudflared has not been downloaded yet.")
            raise TunnelError("download the Cloudflare helper first, then generate the tunnel")
        ready = provider.preflight()
        if not ready.ok:
            return self._require_setup(None, ready)
        with self._lock:
            if self._status.phase == "starting":
                return self._status.as_dict()
            if self._status.phase == "running" and self._status.port == selected and self._alive():
                return self._status.as_dict()
            self._generation += 1
            generation = self._generation
            self._stop_requested.clear()
            self._status = TunnelStatus(
                phase="starting",
                port=selected,
                message="Opening the secure tunnel…",
                provider=provider.id,
                provider_label=provider.label,
                stable_hostname=provider.capabilities.stable_hostname,
                public=provider.capabilities.public,
            )
            self._worker = threading.Thread(
                target=self._run,
                args=(selected, generation),
                name="rainette-tunnel-start",
                daemon=True,
            )
            self._worker.start()
            return self._status.as_dict()

    def stop(self, *, timeout_s: float = 10.0) -> dict:
        """Tear the tunnel down and stop supervising it."""
        with self._lock:
            self._generation += 1
            self._stop_requested.set()
            process = self._process
            handle = self._handle
            self._process = None
            self._handle = None
            supervisor = self._supervisor
            self._supervisor = None
        if handle is not None:
            # A transport whose config lives in a daemon has nothing to kill, so
            # this is the only thing that stops it publishing this computer.
            try:
                self.provider().stop(handle)
            except Exception:
                pass
        self._terminate(process, timeout_s=timeout_s)
        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=timeout_s)
        self._set_status(
            phase="stopped",
            url="",
            message="The tunnel is stopped.",
            setup_action="",
            setup_message="",
            setup_url="",
        )
        return self.status()

    def _alive(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    @staticmethod
    def _terminate(process: subprocess.Popen | None, *, timeout_s: float = 10.0) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=max(0.5, timeout_s))
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        except OSError:
            pass

    # ── worker ────────────────────────────────────────────────────────────

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._stop_requested.is_set()

    def _run(self, port: int, generation: int) -> None:
        try:
            url = self._launch(port, generation)
            if not self._is_current(generation):
                return
            # `on_url` runs *before* the status flips to "running": a caller
            # that polls `status()` (or server.py's own on_url-driven config
            # write) must never be able to observe "running" while the side
            # effect that made the address usable has not happened yet.
            if self._on_url is not None:
                self._on_url(url)
            self._set_status(
                phase="running",
                url=url,
                message="The tunnel is live. Your phone can reach this computer.",
                setup_action="",
                setup_message="",
                setup_url="",
            )
            self._start_supervisor(port, generation)
        except transport.SetupRequired as exc:
            self._require_setup(generation, exc.result)
        except transport.TransportError as exc:
            self._fail(generation, str(exc))
        except Exception as exc:  # a helper we launched misbehaving must not kill the app
            self._fail(generation, f"the tunnel could not be started: {exc}")

    def _require_setup(self, generation: int | None, result: transport.PreflightResult) -> dict:
        """Report an unfinished human step as a checklist, never as a failure."""
        if generation is not None and not self._is_current(generation):
            return self.status()
        with self._lock:
            process = self._process
            self._process = None
            self._handle = None
        self._terminate(process)
        self._set_status(
            phase="setup",
            url="",
            message=result.message or "This option needs a little setup first.",
            setup_action=result.action,
            setup_message=result.message,
            setup_url=result.url,
        )
        return self.status()

    def _fail(self, generation: int, message: str) -> None:
        if not self._is_current(generation):
            return
        with self._lock:
            process = self._process
            self._process = None
            self._handle = None
        self._terminate(process)
        detail = self._log_tail()
        self._set_status(
            phase="error",
            url="",
            message=f"{message} ({detail})" if detail else message,
        )

    def _launch(self, port: int, generation: int) -> str:
        provider = self.provider()
        with self._lock:
            self._log.clear()

        handle = provider.launch(port)
        # The provider's own loops need to know when this launch stopped being
        # the one anybody is waiting for, without taking a generation counter
        # into its signature.
        handle.state["cancelled"] = lambda: not self._is_current(generation)
        with self._lock:
            self._handle = handle
            self._process = handle.process

        url = provider.discover_url(handle, time.monotonic() + _URL_DISCOVERY_TIMEOUT_S)
        if not url:
            raise TunnelError(f"{provider.label} did not produce an address")

        self._set_status(message="Waiting for the tunnel to come online…")
        provider.await_ready(handle, time.monotonic() + _REGISTRATION_TIMEOUT_S)
        if not self._wait_reachable(url, handle, generation):
            raise TunnelError(
                "the tunnel address never answered; check that this computer can reach the internet"
            )
        return url

    @staticmethod
    def _exited(process: subprocess.Popen | None) -> bool:
        """True only for a process that existed and has since stopped."""
        return process is not None and process.poll() is not None

    def _raise_if_exited(self, process: subprocess.Popen | None) -> None:
        if self._exited(process):
            raise TunnelError("the tunnel helper stopped while the address was warming up")

    def _sleep_watching(
        self, seconds: float, process: subprocess.Popen | None, generation: int
    ) -> bool:
        """Sleep in short slices so a stop or a dead helper is noticed promptly."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._is_current(generation) or self._exited(process):
                return False
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
        return True

    def _wait_reachable(
        self, url: str, handle: transport.ProviderHandle, generation: int
    ) -> bool:
        process = handle.process
        # Never probe before the grace period: the first lookup of a name that
        # does not exist yet is what makes it stay unresolvable (see
        # _REACHABLE_GRACE_S).
        if not self._sleep_watching(_REACHABLE_GRACE_S, process, generation):
            self._raise_if_exited(process)
            return False

        provider = self.provider()
        deadline = time.monotonic() + _REACHABLE_TIMEOUT_S
        interval = _PROBE_INTERVAL_S
        while time.monotonic() < deadline:
            if not self._is_current(generation):
                return False
            self._raise_if_exited(process)
            if provider.probe(url):
                return True
            if not self._sleep_watching(interval, process, generation):
                self._raise_if_exited(process)
                return False
            interval = min(interval * _PROBE_BACKOFF, _PROBE_INTERVAL_MAX_S)
        return False

    # ── supervision ───────────────────────────────────────────────────────

    def _start_supervisor(self, port: int, generation: int) -> None:
        supervisor = threading.Thread(
            target=self._supervise,
            args=(port, generation),
            name="rainette-tunnel-supervisor",
            daemon=True,
        )
        # Started *before* it is published: `stop()` reads `self._supervisor`
        # under the lock and, if it is set, immediately joins it.  Publishing
        # the not-yet-started Thread first leaves a window where a `stop()`
        # racing this method grabs that reference and calls `.join()` on a
        # thread that was never `.start()`-ed, which raises
        # "cannot join thread before it is started" instead of tearing down
        # cleanly.
        supervisor.start()
        with self._lock:
            self._supervisor = supervisor

    def _is_down(self) -> bool:
        """Decide whether the transport still works, by two different means.

        A helper process that has exited is the obvious case and the only one
        the old supervisor could see.  The other — a helper that is still alive
        but no longer carrying traffic — looked identical to "healthy" and the
        phone was left to time out.  And a transport whose config lives in a
        daemon has no process at all, so probing is the *only* thing that can
        tell us anything about it.
        """
        with self._lock:
            handle = self._handle
            process = self._process
            url = self._status.url
        if handle is None:
            return False
        if self.provider().capabilities.long_lived_process and self._exited(process):
            return True
        if not url or time.monotonic() < self._next_probe_at:
            return False
        self._next_probe_at = time.monotonic() + _HEALTH_PROBE_INTERVAL_S
        if self.provider().probe(url):
            self._probe_failures = 0
            return False
        # One failed probe is far likelier to be this computer's own network
        # blinking than the tunnel being gone, so only a run of them counts.
        self._probe_failures += 1
        return self._probe_failures >= _HEALTH_PROBE_FAILURES

    def _supervise(self, port: int, generation: int) -> None:
        """Keep the transport up for as long as Rainette is.

        A Quick Tunnel gets a new hostname on every start, so a restart there
        also republishes the address through ``on_url``; a phone that was
        mid-session reconnects by re-scanning, and one that is idle picks it up
        the next time it pairs.  A transport with a stable hostname comes back
        on the *same* address, and republishing that would rewrite the config
        and flash "new address" at the user for nothing — so the republish is
        conditional on the address having actually changed.
        """
        self._probe_failures = 0
        self._next_probe_at = time.monotonic() + _HEALTH_PROBE_INTERVAL_S
        while self._is_current(generation):
            with self._lock:
                handle = self._handle
                process = self._process
                previous_url = self._status.url
            if handle is None:
                return
            if not self._is_down():
                time.sleep(_SUPERVISOR_POLL_S)
                continue

            if not self._is_current(generation):
                return
            self._probe_failures = 0
            self._set_status(phase="starting", url="", message="The tunnel dropped. Reconnecting…")
            # A hung-but-alive helper has to be cleared out before its
            # replacement can bind the same tunnel.
            self._terminate(process)
            time.sleep(_RESTART_BACKOFF_S)
            if not self._is_current(generation):
                return
            try:
                url = self._launch(port, generation)
            except transport.SetupRequired as exc:
                self._require_setup(generation, exc.result)
                return
            except transport.TransportError as exc:
                self._fail(generation, str(exc))
                return
            if not self._is_current(generation):
                return
            self._next_probe_at = time.monotonic() + _HEALTH_PROBE_INTERVAL_S
            changed = bool(url) and url != previous_url
            # Same ordering as the initial launch in `_run`: publish the new
            # address before the status says "running" on it.
            if changed and self._on_url is not None:
                self._on_url(url)
            self._set_status(
                phase="running",
                url=url,
                message="The tunnel is live again on a new address." if changed
                else "The tunnel is live again.",
            )
