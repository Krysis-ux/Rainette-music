"""How this computer becomes reachable from a phone, as a pluggable seam.

Rainette has exactly one thing that works with no setup at all: a Cloudflare
Quick Tunnel.  It is also the worst of the options once you have used it twice,
because the address changes on every restart and every phone has to rescan.
Better options exist, but every one of them costs the user a human step —
installing an app, signing in through a browser, granting a one-time consent —
and a human step that surfaces as "the tunnel failed" is a step nobody takes.

So this module does two things.  It moves the Cloudflare specifics out of
:mod:`tunnel` behind a small provider protocol, and it makes *unfinished setup*
a first-class result rather than an error: :class:`PreflightResult` describes
the next thing the person has to do, in words, with a link, so the settings page
can render a checklist instead of a stack trace.

Nothing here replaces the Quick Tunnel.  ``cloudflare-quick`` remains the
default and behaves exactly as it did before this seam existed; the other
providers are additional options a user may choose, never a migration they are
pushed through.

The module deliberately imports nothing from :mod:`tunnel` or :mod:`server`.
Everything it needs from them — locating cloudflared, downloading it, probing an
address for reachability — arrives by injection, so the dependency runs one way
and the providers stay testable without a running app.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable
from urllib.parse import urlparse

# ── errors ────────────────────────────────────────────────────────────────


class TransportError(RuntimeError):
    """A transport could not be prepared, started, or reached."""


class SetupRequired(TransportError):
    """The user has to do something before this transport can work.

    Carrying a :class:`PreflightResult` rather than only a message is the whole
    point: "install Tailscale" and "approve Funnel for this tailnet" are both
    single clicks *if* the UI is handed the link, and both look like an outage
    if it is handed a sentence.
    """

    def __init__(self, result: "PreflightResult") -> None:
        super().__init__(result.message or "this transport needs to be set up first")
        self.result = result


# ── protocol ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a transport can and cannot promise, in terms the UI can render."""

    stable_hostname: bool = False  # the address survives a restart
    needs_account: bool = False
    needs_domain: bool = False
    auto_installable: bool = False  # may we fetch the binary ourselves
    public: bool = True  # reachable from the open internet
    hides_port: bool = True  # the phone's endpoint carries no port
    long_lived_process: bool = True  # False when the config lives in a daemon

    def as_dict(self) -> dict:
        return {
            "stable_hostname": self.stable_hostname,
            "needs_account": self.needs_account,
            "needs_domain": self.needs_domain,
            "auto_installable": self.auto_installable,
            "public": self.public,
            "hides_port": self.hides_port,
            "long_lived_process": self.long_lived_process,
        }


@dataclass(frozen=True)
class PreflightResult:
    """The next human step, or ``ok`` when there is not one.

    ``can_fix`` is the difference between a checklist and a wizard.  An
    unfinished step that only carries a ``url`` leaves the person to go and do
    something in a browser and then come back and work out what changed; the
    same step with ``can_fix`` set is a button in Rainette that performs it.
    Providers should set it wherever the work is genuinely the app's to do, and
    leave it clear where the step is unavoidably the user's -- creating an
    account, consenting to expose a tailnet, buying a domain.
    """

    ok: bool
    action: str = ""  # "" | "install" | "signup" | "login" | "consent" | "configure" | "provision"
    message: str = ""
    url: str = ""  # a link the UI renders as the next step
    can_fix: bool = False  # Rainette itself can carry this step out
    fix_label: str = ""  # what the button that does it should say
    detail: str = ""  # optional second line, for the "why" behind the step

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "setup_action": self.action,
            "setup_message": self.message,
            "setup_url": self.url,
            "setup_can_fix": self.can_fix,
            "setup_fix_label": self.fix_label,
            "setup_detail": self.detail,
        }


@dataclass
class ProviderHandle:
    """Whatever a running transport needs to be stopped again.

    ``process`` is ``None`` for providers whose configuration lives in a daemon
    (Tailscale) or nowhere at all (manual).  That is exactly why ``stop`` takes
    a handle instead of "kill the process": without it those providers could not
    participate in the supervisor at all.
    """

    process: subprocess.Popen | None = None
    state: dict = field(default_factory=dict)

    def cancelled(self) -> bool:
        """True once the supervisor has moved on from this launch.

        The check is injected through ``state`` rather than passed as an
        argument so the protocol's method signatures stay as narrow as they
        read.  A handle nobody wired up simply never cancels.
        """
        check = self.state.get("cancelled")
        try:
            return bool(check()) if callable(check) else False
        except Exception:
            return False


@runtime_checkable
class TransportProvider(Protocol):
    id: str
    label: str
    capabilities: ProviderCapabilities

    def ensure_binary(self, progress: Callable[[str], None] | None) -> Path | None: ...
    def preflight(self) -> PreflightResult: ...
    def launch(self, port: int) -> ProviderHandle: ...
    def discover_url(self, handle: ProviderHandle, deadline: float) -> str: ...
    def probe(self, url: str) -> bool: ...
    def stop(self, handle: ProviderHandle) -> None: ...
    def owns_url(self, url: str) -> bool: ...


# ── identifiers ───────────────────────────────────────────────────────────

CLOUDFLARE_QUICK = "cloudflare-quick"
TAILSCALE_SERVE = "tailscale-serve"
TAILSCALE_FUNNEL = "tailscale-funnel"
CLOUDFLARE_NAMED = "cloudflare-named"
MANUAL = "manual"

DEFAULT_PROVIDER = CLOUDFLARE_QUICK

TRANSPORT_CONFIG_FILENAME = "transport-config.json"

_QUICK_TUNNEL_HOST_SUFFIX = ".trycloudflare.com"
_TAILNET_HOST_SUFFIX = ".ts.net"

_QUICK_TUNNEL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
_REGISTERED_MARKER = "registered tunnel connection"
_FUNNEL_CONSENT_RE = re.compile(r"https://login\.tailscale\.com/f/funnel\?\S+")
_TAILSCALE_AUTH_RE = re.compile(r"https://login\.tailscale\.com/\S+")
_TAILSCALE_DOWNLOAD_URL = "https://tailscale.com/download"
_TAILSCALE_DNS_ADMIN_URL = "https://login.tailscale.com/admin/dns"
_TAILSCALE_LOGIN_URL = "https://login.tailscale.com/"
_CLOUDFLARE_LOGIN_URL = "https://dash.cloudflare.com/"
_CLOUDFLARE_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"
_CLOUDFLARE_ADD_SITE_URL = "https://dash.cloudflare.com/?to=/:account/add-site"
_CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

# `cloudflared tunnel login` blocks until the browser round-trip finishes, so it
# is run on a thread and polled for its artefact rather than waited on.
_CLOUDFLARE_LOGIN_TIMEOUT_S = 300.0
_CLOUDFLARE_LOGIN_POLL_S = 1.0
_CLOUDFLARE_API_TIMEOUT_S = 20.0
# Creating a tunnel and its DNS record are two quick API calls behind the CLI.
_CLOUDFLARE_PROVISION_TIMEOUT_S = 90.0

# The subdomain Rainette suggests on the user's own zone. Nothing depends on the
# value; it only has to be memorable and unlikely to collide with a real site.
_DEFAULT_SUBDOMAIN = "music"

# `tailscale serve`/`funnel` reconfigure a daemon and return; they never take
# this long unless something is badly wrong.
_TAILSCALE_COMMAND_TIMEOUT_S = 60.0
_TAILSCALE_POLL_S = 1.0
# `tailscale up` waits for a browser round-trip, so it gets the same patience as
# the Cloudflare sign-in rather than the command timeout.
_TAILSCALE_LOGIN_TIMEOUT_S = 300.0
# How long to give the daemon to come up after its app is launched.
_TAILSCALE_DAEMON_WAIT_S = 20.0


def _host_of(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


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


# ── base ──────────────────────────────────────────────────────────────────


class _BaseProvider:
    """Shared plumbing; every provider below is one of these.

    Callables are injected rather than imported so this module never depends on
    :mod:`tunnel`: ``probe`` is the reachability check, ``record`` is the
    supervisor's log sink, and the cloudflared locator/installer come from the
    module that owns the download policy.
    """

    id = ""
    label = ""
    description = ""
    capabilities = ProviderCapabilities()
    ready_marker = ""  # a log line that means "the edge accepted us", if any

    def __init__(
        self,
        *,
        config: Mapping | None = None,
        probe: Callable[[str], bool] | None = None,
        record: Callable[[str], None] | None = None,
    ) -> None:
        self._config = dict(config or {})
        self._probe = probe or (lambda url: False)
        self._record = record or (lambda line: None)

    # -- defaults every provider may keep --------------------------------

    def ensure_binary(self, progress: Callable[[str], None] | None = None) -> Path | None:
        return None

    def preflight(self) -> PreflightResult:
        return PreflightResult(ok=True)

    def launch(self, port: int) -> ProviderHandle:
        return ProviderHandle()

    def discover_url(self, handle: ProviderHandle, deadline: float) -> str:
        return ""

    def await_ready(self, handle: ProviderHandle, deadline: float) -> None:
        """Optional gate between "launched" and "worth probing"."""
        return None

    def probe(self, url: str) -> bool:
        return self._probe(url)

    def stop(self, handle: ProviderHandle) -> None:
        return None

    def owns_url(self, url: str) -> bool:
        return False

    # -- helpers ----------------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            **self.capabilities.as_dict(),
        }

    def _run(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            # Explicit, not left to locale.getpreferredencoding(): on Windows
            # that is commonly a legacy code page rather than UTF-8, and a
            # helper that prints anything outside it would otherwise raise
            # UnicodeDecodeError -- which is neither OSError nor
            # SubprocessError, so it would reach callers uncaught.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window_kwargs(),
        )


class _ProcessProvider(_BaseProvider):
    """A provider whose transport *is* a child process printing to a log.

    The URL is discovered by watching that log, which means the pump has to
    start with the process and outlive the discovery loop.
    """

    url_pattern: re.Pattern[str] | None = None

    def _start_pump(self, handle: ProviderHandle) -> None:
        process = handle.process
        if process is None or process.stdout is None:
            return
        found = handle.state.setdefault("found", {})
        lines: list[str] = handle.state.setdefault("lines", [])

        def pump() -> None:
            stream = process.stdout
            if stream is None:
                return
            for line in stream:
                self._record(line)
                lines.append(line)
                del lines[:-200]
                if self.url_pattern is not None and "url" not in found:
                    match = self.url_pattern.search(line)
                    if match:
                        found["url"] = match.group(0)

        threading.Thread(target=pump, name="rainette-transport-log", daemon=True).start()

    def discover_url(self, handle: ProviderHandle, deadline: float) -> str:
        process = handle.process
        found = handle.state.setdefault("found", {})
        while time.monotonic() < deadline:
            if "url" in found:
                return found["url"]
            if process is not None and process.poll() is not None:
                raise TransportError("the tunnel helper exited before it opened a tunnel")
            if handle.cancelled():
                return ""
            time.sleep(0.25)
        return found.get("url", "")

    def await_ready(self, handle: ProviderHandle, deadline: float) -> None:
        """Wait for the helper to report an edge connection, if it says so.

        Advisory on purpose: the log wording is not a stable interface, so a
        miss falls through to the reachability probe rather than failing.
        """
        if not self.ready_marker:
            return
        marker = self.ready_marker.lower()
        process = handle.process
        lines: list[str] = handle.state.setdefault("lines", [])
        while time.monotonic() < deadline:
            if handle.cancelled() or (process is not None and process.poll() is not None):
                return
            if any(marker in line.lower() for line in tuple(lines)):
                return
            time.sleep(0.5)


# ── cloudflare ────────────────────────────────────────────────────────────


class _CloudflareProvider(_ProcessProvider):
    """Common ground between the Quick Tunnel and a user's named tunnel."""

    ready_marker = _REGISTERED_MARKER

    def __init__(
        self,
        *,
        locate: Callable[[], Path | None] | None = None,
        install: Callable[[Callable[[str], None] | None], Path] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._locate = locate or (lambda: None)
        self._install = install
        self._binary: Path | None = None

    def ensure_binary(self, progress: Callable[[str], None] | None = None) -> Path | None:
        """Locate cloudflared. Downloading stays a separate, named user step.

        The one action that reaches the network on the user's behalf must be
        something they chose, not a side effect of pressing "generate".
        """
        if self._binary is not None:
            return self._binary
        self._binary = self._locate()
        return self._binary

    def install_binary(self, progress: Callable[[str], None] | None = None) -> Path:
        if self._install is None:
            raise TransportError("this transport cannot install its helper")
        path = self._install(progress)
        self._binary = Path(path)
        return self._binary

    def _popen(self, command: list[str]) -> subprocess.Popen:
        try:
            return subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                **_no_window_kwargs(),
            )
        except OSError as exc:
            raise TransportError(f"the tunnel helper would not run: {exc}") from exc

    def stop(self, handle: ProviderHandle) -> None:
        return None  # the supervisor terminates the process it owns


class CloudflareQuickProvider(_CloudflareProvider):
    """Today's behaviour, unchanged, behind the provider seam.

    Zero setup and a fresh ``*.trycloudflare.com`` hostname on every start.  It
    is the default because it is the only option that asks the user for nothing,
    and it must keep working exactly as it always has.
    """

    id = CLOUDFLARE_QUICK
    label = "Limited tunnel"
    description = "No setup at all. The address changes every time Rainette restarts, so your phone has to rescan the code."
    capabilities = ProviderCapabilities(
        stable_hostname=False,
        needs_account=False,
        needs_domain=False,
        auto_installable=True,
        public=True,
        hides_port=True,
        long_lived_process=True,
    )
    url_pattern = _QUICK_TUNNEL_RE

    def launch(self, port: int) -> ProviderHandle:
        binary = self.ensure_binary()
        if binary is None:
            raise TransportError("download the Cloudflare helper first, then generate the tunnel")
        handle = ProviderHandle(
            process=self._popen([
                str(binary),
                "tunnel",
                "--no-autoupdate",
                "--loglevel", "info",
                "--url", f"http://127.0.0.1:{port}",
            ])
        )
        self._start_pump(handle)
        return handle

    def discover_url(self, handle: ProviderHandle, deadline: float) -> str:
        url = super().discover_url(handle, deadline)
        if not url and not handle.cancelled():
            raise TransportError("Cloudflare did not hand out a tunnel address")
        return url

    def owns_url(self, url: str) -> bool:
        return _host_of(url).endswith(_QUICK_TUNNEL_HOST_SUFFIX)


class CloudflareNamedProvider(_CloudflareProvider):
    """A tunnel the user owns, on a domain already sitting on Cloudflare.

    Everything this provider needs beyond an account and a domain, it does
    itself.  ``cloudflared`` already knows how to open a browser for consent
    (``tunnel login``), mint a tunnel (``tunnel create``) and write the DNS
    record that points at it (``tunnel route dns``); the only reason those ever
    had to be typed into a terminal is that nothing was calling them.  So the
    provider drives them, and :meth:`preflight` becomes a wizard: at every
    moment it reports the single next step and whether Rainette can take it.

    Two steps are genuinely not ours and stay as links: creating the account,
    and putting a domain on Cloudflare.  Pretending otherwise would mean
    automating somebody's signup, which is neither possible nor desirable.
    """

    id = CLOUDFLARE_NAMED
    label = "Your own permanent address"
    description = "A permanent address on a domain you own, with a real certificate. Needs a free Cloudflare account and a domain on Cloudflare — Rainette sets up the rest for you."
    capabilities = ProviderCapabilities(
        stable_hostname=True,
        needs_account=True,
        needs_domain=True,
        auto_installable=True,
        public=True,
        hides_port=True,
        long_lived_process=True,
    )

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._login_lock = threading.Lock()
        self._login_thread: threading.Thread | None = None
        self._login_error = ""
        self._login_started_at = 0.0

    @property
    def hostname(self) -> str:
        return str(self._config.get("hostname") or "").strip().rstrip("/")

    @property
    def tunnel_name(self) -> str:
        return str(self._config.get("tunnel_name") or "").strip()

    # -- the artefacts cloudflared leaves behind ---------------------------

    @staticmethod
    def cert_path() -> Path:
        """Where ``cloudflared tunnel login`` writes the account certificate."""
        return Path.home() / ".cloudflared" / "cert.pem"

    def signed_in(self) -> bool:
        return self.cert_path().is_file()

    def login_in_progress(self) -> bool:
        thread = self._login_thread
        return bool(thread and thread.is_alive())

    # -- step: sign in -----------------------------------------------------

    def begin_login(self) -> dict:
        """Run ``cloudflared tunnel login``, which opens the browser itself.

        The command blocks until the person has picked a domain in Cloudflare's
        UI, so it cannot be waited on inline without freezing the settings page.
        It goes on a thread; progress is observed through the certificate file
        it writes, which is the same thing every later step checks anyway.
        """
        if self.signed_in():
            return {"ok": True, "already": True}
        binary = self.ensure_binary()
        if binary is None:
            raise TransportError("download the Cloudflare helper first")
        with self._login_lock:
            if self.login_in_progress():
                return {"ok": True, "already": False, "pending": True}
            self._login_error = ""
            self._login_started_at = time.monotonic()
            thread = threading.Thread(
                target=self._login_worker,
                args=(str(binary),),
                name="rainette-cloudflare-login",
                daemon=True,
            )
            self._login_thread = thread
            thread.start()
        return {"ok": True, "already": False, "pending": True}

    def _login_worker(self, binary: str) -> None:
        try:
            completed = self._run([binary, "tunnel", "login"], timeout=_CLOUDFLARE_LOGIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._login_error = "Signing in to Cloudflare timed out. Try again."
            return
        except OSError as exc:
            self._login_error = f"The Cloudflare helper would not run: {exc}"
            return
        for line in f"{completed.stdout or ''}\n{completed.stderr or ''}".splitlines():
            if line.strip():
                self._record(line)
        if not self.signed_in():
            detail = _first_line(completed.stderr) or _first_line(completed.stdout)
            self._login_error = (
                f"Cloudflare did not finish signing in: {detail}" if detail
                else "Cloudflare did not finish signing in."
            )

    # -- step: work out which domain they picked ---------------------------

    def _cert_credentials(self) -> dict:
        """Pull the zone and account ids out of the certificate cloudflared wrote.

        ``cert.pem`` carries an extra PEM block holding a small JSON blob.  It is
        the only place the chosen zone is recorded locally, and reading it is
        what lets Rainette suggest a hostname instead of asking for one.
        """
        try:
            raw = self.cert_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        match = re.search(
            r"-----BEGIN ARGO TUNNEL TOKEN-----(.*?)-----END ARGO TUNNEL TOKEN-----",
            raw,
            re.DOTALL,
        )
        if not match:
            return {}
        try:
            payload = json.loads(base64.b64decode("".join(match.group(1).split())))
        except (ValueError, binascii.Error):
            return {}
        return payload if isinstance(payload, dict) else {}

    def zone_name(self) -> str:
        """The domain the user chose while signing in, or "" if it cannot be read.

        Best effort on purpose.  A failure here costs a prefilled text box, not
        the feature: :meth:`preflight` falls back to asking for the hostname.
        """
        credentials = self._cert_credentials()
        zone_id = str(credentials.get("zoneID") or "").strip()
        token = str(credentials.get("apiToken") or "").strip()
        if not zone_id or not token:
            return ""
        request = urllib.request.Request(
            f"{_CLOUDFLARE_API_BASE}/zones/{zone_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_CLOUDFLARE_API_TIMEOUT_S) as response:
                payload = json.loads(response.read(1_000_000) or b"{}")
        except (urllib.error.URLError, OSError, ValueError):
            return ""
        result = payload.get("result") if isinstance(payload, dict) else None
        name = (result or {}).get("name") if isinstance(result, dict) else ""
        return str(name or "").strip().lower()

    def suggested_hostname(self) -> str:
        zone = self.zone_name()
        return f"{_DEFAULT_SUBDOMAIN}.{zone}" if zone else ""

    @staticmethod
    def suggested_tunnel_name() -> str:
        """A name that says which computer it is, without leaking anything odd."""
        try:
            raw = socket.gethostname().split(".")[0]
        except OSError:
            raw = ""
        slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
        return f"rainette-{slug}" if slug else "rainette"

    # -- step: create the tunnel and point the name at it ------------------

    def provision(self, hostname: str = "", tunnel_name: str = "") -> dict:
        """Create the tunnel and its DNS record, tolerating both already existing.

        Re-running this is the normal case, not the exception: somebody who
        pressed the button, quit, and came back should land on "ready" rather
        than on "a tunnel with that name already exists".
        """
        binary = self.ensure_binary()
        if binary is None:
            raise TransportError("download the Cloudflare helper first")
        if not self.signed_in():
            raise TransportError("sign in to Cloudflare first")
        name = str(tunnel_name or "").strip() or self.tunnel_name or self.suggested_tunnel_name()
        host = str(hostname or "").strip().rstrip("/") or self.hostname or self.suggested_hostname()
        if not host:
            raise TransportError(
                "Rainette could not work out your domain. Enter the address you want to use."
            )
        self._create_tunnel(str(binary), name)
        self._route_dns(str(binary), name, host)
        self._config["tunnel_name"] = name
        self._config["hostname"] = host
        return {"ok": True, "tunnel_name": name, "hostname": host}

    def _create_tunnel(self, binary: str, name: str) -> None:
        try:
            completed = self._run(
                [binary, "tunnel", "create", name],
                timeout=_CLOUDFLARE_PROVISION_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError("Creating the Cloudflare tunnel took too long") from exc
        except OSError as exc:
            raise TransportError(f"the Cloudflare helper would not run: {exc}") from exc
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        for line in output.splitlines():
            if line.strip():
                self._record(line)
        if completed.returncode == 0 or "already exists" in output.lower():
            return
        detail = _first_line(completed.stderr) or _first_line(completed.stdout)
        raise TransportError(
            f"Cloudflare would not create the tunnel: {detail}" if detail
            else "Cloudflare would not create the tunnel"
        )

    def _route_dns(self, binary: str, name: str, host: str) -> None:
        try:
            completed = self._run(
                [binary, "tunnel", "route", "dns", name, host],
                timeout=_CLOUDFLARE_PROVISION_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError("Setting up the Cloudflare address took too long") from exc
        except OSError as exc:
            raise TransportError(f"the Cloudflare helper would not run: {exc}") from exc
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        for line in output.splitlines():
            if line.strip():
                self._record(line)
        if completed.returncode == 0:
            return
        lowered = output.lower()
        # An existing record that already points at this tunnel is the desired
        # end state, so only a record owned by something else is a real failure.
        if "already exists" in lowered and name.lower() in lowered:
            return
        if "already configured to point to tunnel" in lowered:
            return
        detail = _first_line(completed.stderr) or _first_line(completed.stdout)
        raise TransportError(
            f"Cloudflare would not point {host} at this computer: {detail}" if detail
            else f"Cloudflare would not point {host} at this computer"
        )

    # -- the wizard ---------------------------------------------------------

    def preflight(self) -> PreflightResult:
        if self.ensure_binary() is None:
            return PreflightResult(
                ok=False,
                action="install",
                message="Rainette needs Cloudflare's helper on this computer.",
                can_fix=True,
                fix_label="Download the Cloudflare helper",
                detail="A one-off download, about 40 MB. Nothing leaves this computer yet.",
            )
        if self._login_error:
            return PreflightResult(
                ok=False,
                action="login",
                message=self._login_error,
                url=_CLOUDFLARE_SIGNUP_URL,
                can_fix=True,
                fix_label="Try signing in again",
            )
        if self.login_in_progress():
            return PreflightResult(
                ok=False,
                action="login",
                message="Finish signing in to Cloudflare in the browser window that just opened, then pick the domain you want to use.",
                detail="This page updates itself as soon as Cloudflare is done.",
            )
        if not self.signed_in():
            return PreflightResult(
                ok=False,
                action="login",
                message="Sign in to Cloudflare and choose the domain your phone should use.",
                url=_CLOUDFLARE_SIGNUP_URL,
                can_fix=True,
                fix_label="Sign in to Cloudflare",
                detail="No account yet? Creating one is free — you will also need a domain name added to Cloudflare.",
            )
        if not self.tunnel_name or not self.hostname:
            suggestion = self.suggested_hostname()
            return PreflightResult(
                ok=False,
                action="provision",
                message=(
                    f"Rainette will set up {suggestion} for this computer."
                    if suggestion
                    else "Rainette will create your tunnel. Enter the address you would like to use."
                ),
                can_fix=bool(suggestion),
                fix_label="Create my address",
                detail="This creates a Cloudflare tunnel and points the address at this computer.",
            )
        return PreflightResult(ok=True, message=f"Ready. Your phone will use https://{self.hostname}")

    def launch(self, port: int) -> ProviderHandle:
        ready = self.preflight()
        if not ready.ok:
            raise SetupRequired(ready)
        binary = self.ensure_binary()
        if binary is None:
            raise TransportError("download the Cloudflare helper first, then generate the tunnel")
        handle = ProviderHandle(
            process=self._popen([
                str(binary),
                "tunnel",
                "--no-autoupdate",
                "run",
                "--url", f"http://127.0.0.1:{port}",
                self.tunnel_name,
            ]),
            state={"hostname": self.hostname},
        )
        self._start_pump(handle)
        return handle

    def discover_url(self, handle: ProviderHandle, deadline: float) -> str:
        """Configured, never scraped — a named tunnel's address is known up front."""
        hostname = self.hostname
        if not hostname:
            raise TransportError("no hostname is configured for this Cloudflare tunnel")
        return f"https://{hostname}"

    def owns_url(self, url: str) -> bool:
        hostname = self.hostname
        return bool(hostname) and _host_of(url) == _host_of(f"https://{hostname}")


# ── tailscale ─────────────────────────────────────────────────────────────

_TAILSCALE_PATHS = (
    "tailscale",  # PATH (brew, linux, standalone)
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",  # macOS App Store build
    "/usr/local/bin/tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
)


class _TailscaleProvider(_BaseProvider):
    """Config lives in the Tailscale daemon, so there is no process to own.

    That is the case ``long_lived_process = False`` exists for: ``launch``
    returns as soon as the daemon has been reconfigured, and supervision becomes
    "keep probing, and rewrite the config if it stops answering".
    """

    mode = "serve"
    capabilities = ProviderCapabilities(
        stable_hostname=True,
        needs_account=True,
        needs_domain=False,
        auto_installable=False,
        public=False,
        hides_port=True,
        long_lived_process=False,
    )

    def __init__(self, *, locate: Callable[[], Path | None] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._locate = locate or _locate_tailscale
        self._binary: Path | None = None
        self._login_lock = threading.Lock()
        self._login_thread: threading.Thread | None = None
        self._login_error = ""

    def ensure_binary(self, progress: Callable[[str], None] | None = None) -> Path | None:
        """Locate only. Tailscale cannot be installed silently and should not be.

        It is a system-level VPN client; fetching and running one on somebody's
        behalf is not a thing an app should do quietly.
        """
        if self._binary is None:
            self._binary = self._locate()
        return self._binary

    # -- daemon interrogation --------------------------------------------

    def _status_json(self) -> dict | None:
        binary = self.ensure_binary()
        if binary is None:
            return None
        try:
            completed = self._run([str(binary), "status", "--json"], timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode not in (0, 1):
            # `tailscale status` exits 1 while logged out but still prints JSON.
            return None
        try:
            payload = json.loads(completed.stdout or "{}")
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _dns_name(self, status: Mapping | None = None) -> str:
        payload = status if status is not None else self._status_json()
        node = (payload or {}).get("Self") or {}
        return str(node.get("DNSName") or "").strip().rstrip(".")

    # -- step: bring the daemon up and signed in ---------------------------

    def login_in_progress(self) -> bool:
        thread = self._login_thread
        return bool(thread and thread.is_alive())

    def begin_login(self) -> dict:
        """Run ``tailscale up``, which is what makes the daemon mint an auth URL.

        Logged out, ``tailscale status`` reports no ``AuthURL`` at all -- the
        daemon only produces one once something has asked it to come up.  So the
        button that used to send people to a login page they could do nothing
        with instead starts the thing that generates the link, and the next
        poll of :meth:`preflight` has a real URL to offer.
        """
        binary = self.ensure_binary()
        if binary is None:
            raise TransportError("install Tailscale first")
        with self._login_lock:
            if self.login_in_progress():
                return {"ok": True, "pending": True}
            self._login_error = ""
            thread = threading.Thread(
                target=self._login_worker,
                args=(str(binary),),
                name="rainette-tailscale-login",
                daemon=True,
            )
            self._login_thread = thread
            thread.start()
        return {"ok": True, "pending": True}

    def _wake_daemon(self) -> None:
        """Start the Tailscale app if its daemon is not answering.

        On macOS the CLI is a client of a daemon the *app* owns, so with the app
        quit `tailscale status` returns nothing usable and `tailscale up` has
        nothing to talk to either. Telling the user to "open the Tailscale app
        once" is a step Rainette can simply take, and the button that offered to
        connect was otherwise guaranteed to fail before it did anything.
        """
        if not sys.platform == "darwin" or self._status_json() is not None:
            return
        try:
            subprocess.run(["/usr/bin/open", "-a", "Tailscale"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return
        # The daemon comes up a moment after the app does; without this the
        # `up` below would race it and fail for the reason just fixed.
        deadline = time.monotonic() + _TAILSCALE_DAEMON_WAIT_S
        while time.monotonic() < deadline:
            if self._status_json() is not None:
                return
            time.sleep(_TAILSCALE_POLL_S)

    def _login_worker(self, binary: str) -> None:
        self._wake_daemon()
        try:
            completed = self._run([binary, "up"], timeout=_TAILSCALE_LOGIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # Not an error: `tailscale up` waits for the browser, and the daemon
            # keeps the pending login alive after the command gives up.
            return
        except OSError as exc:
            self._login_error = f"Tailscale would not start: {exc}"
            return
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        for line in output.splitlines():
            if line.strip():
                self._record(line)
        if completed.returncode != 0:
            # A printed auth URL is the normal path, not a failure to report --
            # and it lands a line or two below "To authenticate, visit:", so the
            # whole output has to be searched rather than only its first line.
            if _TAILSCALE_AUTH_RE.search(output):
                return
            detail = _first_line(completed.stderr) or _first_line(completed.stdout)
            if detail:
                self._login_error = f"Tailscale could not sign in: {detail}"

    def preflight(self) -> PreflightResult:
        if self.ensure_binary() is None:
            return PreflightResult(
                ok=False,
                action="install",
                message="Rainette needs the free Tailscale app on this computer.",
                url=_TAILSCALE_DOWNLOAD_URL,
                detail="Install it on your phone too — that pair is what lets the two talk directly.",
            )
        if self._login_error:
            return PreflightResult(
                ok=False,
                action="login",
                message=self._login_error,
                url=_TAILSCALE_LOGIN_URL,
                can_fix=True,
                fix_label="Try connecting again",
            )
        status = self._status_json()
        if status is None:
            return PreflightResult(
                ok=False,
                action="login",
                message="Tailscale is installed but not running yet.",
                can_fix=True,
                fix_label="Start Tailscale",
                detail="Rainette will open it for you and connect this computer.",
            )
        backend = str(status.get("BackendState") or "")
        if backend != "Running":
            auth_url = str(status.get("AuthURL") or "")
            if auth_url:
                # The daemon is waiting on the browser; the link is the whole step.
                return PreflightResult(
                    ok=False,
                    action="login",
                    message="Finish connecting Tailscale in your browser, then come back.",
                    url=auth_url,
                    detail="This page updates itself as soon as Tailscale is connected.",
                )
            if self.login_in_progress():
                return PreflightResult(
                    ok=False,
                    action="login",
                    message="Starting Tailscale on this computer…",
                    detail="A browser window will open for you to approve it.",
                )
            return PreflightResult(
                ok=False,
                action="login",
                message="Connect this computer to your Tailscale network.",
                url=_TAILSCALE_LOGIN_URL,
                can_fix=True,
                fix_label="Connect Tailscale",
                detail="Rainette will open a browser window for you to approve it. The account is free.",
            )
        dns_name = self._dns_name(status)
        if not dns_name:
            return PreflightResult(
                ok=False,
                action="consent",
                message="Turn on MagicDNS for your Tailscale network so this computer has a name.",
                url=_TAILSCALE_DNS_ADMIN_URL,
                detail="One switch on Tailscale's own settings page. Rainette cannot flip it for you.",
            )
        if not (status.get("CertDomains") or []):
            # No cert domains means HTTPS is off for the whole tailnet, and
            # without a real certificate the phone cannot connect at all.
            return PreflightResult(
                ok=False,
                action="consent",
                message="Turn on HTTPS certificates for your Tailscale network, then come back.",
                url=_TAILSCALE_DNS_ADMIN_URL,
                detail="The switch is just below MagicDNS on the same page.",
            )
        return PreflightResult(ok=True, message=f"Ready. Your phone will use https://{dns_name}")

    # -- lifecycle --------------------------------------------------------

    def launch(self, port: int) -> ProviderHandle:
        ready = self.preflight()
        if not ready.ok:
            raise SetupRequired(ready)
        binary = self.ensure_binary()
        # The 443/8443/10000 restriction is on the *public* listener, not on the
        # local target, so the pinned companion port needs no special handling.
        command = [str(binary), self.mode, "--bg", "--https=443", f"http://127.0.0.1:{port}"]
        try:
            completed = self._run(command, timeout=_TAILSCALE_COMMAND_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"`tailscale {self.mode}` did not finish in time") from exc
        except OSError as exc:
            raise TransportError(f"the Tailscale command would not run: {exc}") from exc
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        for line in output.splitlines():
            self._record(line)
        if completed.returncode != 0:
            consent = _FUNNEL_CONSENT_RE.search(output)
            if consent:
                # A fresh tailnet has not agreed to expose anything publicly yet.
                # This is the single most likely first-run stumble, and it is a
                # button, not an outage.
                raise SetupRequired(PreflightResult(
                    ok=False,
                    action="consent",
                    message="Allow Funnel for this tailnet once, then come back.",
                    url=consent.group(0),
                ))
            detail = _first_line(completed.stderr) or _first_line(completed.stdout)
            raise TransportError(
                f"Tailscale could not publish this computer: {detail}" if detail
                else "Tailscale could not publish this computer"
            )
        return ProviderHandle(process=None, state={"mode": self.mode, "port": int(port)})

    def discover_url(self, handle: ProviderHandle, deadline: float) -> str:
        """Read the node's own name. No log scraping — the daemon knows it."""
        while True:
            dns_name = self._dns_name()
            if dns_name:
                return f"https://{dns_name}"
            if handle.cancelled() or time.monotonic() >= deadline:
                raise TransportError("Tailscale did not report a name for this computer")
            time.sleep(_TAILSCALE_POLL_S)

    def stop(self, handle: ProviderHandle) -> None:
        binary = self.ensure_binary()
        if binary is None:
            return
        try:
            self._run([str(binary), self.mode, "--https=443", "off"], timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    def owns_url(self, url: str) -> bool:
        return _host_of(url).endswith(_TAILNET_HOST_SUFFIX)


class TailscaleServeProvider(_TailscaleProvider):
    """The recommended option: a stable name that is never on the public internet.

    This is also the closest thing to "just use the local network" that a
    browser will actually accept.  On the same Wi-Fi, Tailscale connects the two
    devices directly and the audio never leaves the building -- but unlike a
    bare ``http://192.168.x.x`` address it carries a real certificate, so the
    phone client stays a secure context and keeps the two things that depend on
    one: installing to the home screen, and working offline.
    """

    id = TAILSCALE_SERVE
    mode = "serve"
    label = "Direct on your network"
    description = "Recommended. On the same Wi-Fi your phone talks straight to this computer, so it is fast and nothing leaves your network. The address never changes, and your phone only scans a code once. Needs the free Tailscale app here and on your phone."


class TailscaleFunnelProvider(_TailscaleProvider):
    """Same address, but publicly reachable — for a guest who will not install anything."""

    id = TAILSCALE_FUNNEL
    mode = "funnel"
    label = "Direct, plus reachable anywhere"
    description = "The same permanent address, but it also works when you are away from home. Only needed for a phone that will not install Tailscale, or for listening on mobile data."
    capabilities = replace(_TailscaleProvider.capabilities, public=True)


def _locate_tailscale() -> Path | None:
    for candidate in _TAILSCALE_PATHS:
        found = (
            candidate if Path(candidate).is_file() else None
        ) if os.path.isabs(candidate) else shutil.which(candidate)
        if found:
            return Path(found)
    return None


# ── manual ────────────────────────────────────────────────────────────────


class ManualProvider(_BaseProvider):
    """An address the user brings themselves — a reverse proxy, a VPS, anything.

    This has always half-existed as "whatever is in ``public_url`` that Rainette
    did not mint".  Making it a provider gives it the one thing it never had:
    supervision.  Until now a user's own proxy going down was invisible here.
    """

    id = MANUAL
    label = "Bring your own address"
    description = "You already have an HTTPS address that reaches this computer — a reverse proxy, a VPS, or a tunnel you run yourself."
    capabilities = ProviderCapabilities(
        stable_hostname=True,
        needs_account=False,
        needs_domain=False,
        auto_installable=False,
        public=True,
        hides_port=False,
        long_lived_process=False,
    )

    @property
    def public_url(self) -> str:
        return str(self._config.get("public_url") or "").strip().rstrip("/")

    def preflight(self) -> PreflightResult:
        address = self.public_url
        if not address:
            return PreflightResult(
                ok=False,
                action="configure",
                message="Enter the HTTPS address that already reaches this computer.",
            )
        parsed = urlparse(address)
        local = (parsed.hostname or "") in {"localhost", "127.0.0.1", "::1"}
        # An HTTPS page cannot call a plain-HTTP endpoint on another device, so
        # this has to fail here rather than at pairing time.
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            return PreflightResult(
                ok=False,
                action="configure",
                message="That address must use HTTPS, or your phone's browser will refuse to call it.",
            )
        return PreflightResult(ok=True, message=f"Ready. Your phone will use {address}")

    def launch(self, port: int) -> ProviderHandle:
        ready = self.preflight()
        if not ready.ok:
            raise SetupRequired(ready)
        return ProviderHandle(process=None, state={"public_url": self.public_url})

    def discover_url(self, handle: ProviderHandle, deadline: float) -> str:
        return self.public_url

    def owns_url(self, url: str) -> bool:
        """Never. A user-supplied address is never cleared on our say-so."""
        return False


# ── registry ──────────────────────────────────────────────────────────────

_PROVIDER_CLASSES: dict[str, type[_BaseProvider]] = {
    CLOUDFLARE_QUICK: CloudflareQuickProvider,
    TAILSCALE_SERVE: TailscaleServeProvider,
    TAILSCALE_FUNNEL: TailscaleFunnelProvider,
    CLOUDFLARE_NAMED: CloudflareNamedProvider,
    MANUAL: ManualProvider,
}

# Order is the order the picker shows: what works with no setup first, then the
# recommended upgrade, then the ones that ask progressively more of the user.
PROVIDER_IDS: tuple[str, ...] = (
    CLOUDFLARE_QUICK,
    TAILSCALE_SERVE,
    TAILSCALE_FUNNEL,
    CLOUDFLARE_NAMED,
    MANUAL,
)


def known_provider(provider_id: str) -> bool:
    return str(provider_id or "") in _PROVIDER_CLASSES


def provider_class(provider_id: str) -> type[_BaseProvider]:
    return _PROVIDER_CLASSES.get(str(provider_id or ""), CloudflareQuickProvider)


def provider_capabilities(provider_id: str) -> ProviderCapabilities:
    return provider_class(provider_id).capabilities


def catalogue() -> list[dict]:
    """Every provider the UI may offer, in picker order."""
    return [
        {
            "id": pid,
            "label": _PROVIDER_CLASSES[pid].label,
            "description": _PROVIDER_CLASSES[pid].description,
            "recommended": pid == TAILSCALE_SERVE,
            "default": pid == DEFAULT_PROVIDER,
            **_PROVIDER_CLASSES[pid].capabilities.as_dict(),
        }
        for pid in PROVIDER_IDS
    ]


def build_provider(
    provider_id: str,
    *,
    config: Mapping | None = None,
    probe: Callable[[str], bool] | None = None,
    record: Callable[[str], None] | None = None,
    locate_cloudflared: Callable[[], Path | None] | None = None,
    install_cloudflared: Callable[[Callable[[str], None] | None], Path] | None = None,
    locate_tailscale: Callable[[], Path | None] | None = None,
) -> _BaseProvider:
    """Construct a provider with the host application's capabilities injected."""
    cls = provider_class(provider_id)
    kwargs: dict = {"config": config, "probe": probe, "record": record}
    if issubclass(cls, _CloudflareProvider):
        kwargs["locate"] = locate_cloudflared
        kwargs["install"] = install_cloudflared
    elif issubclass(cls, _TailscaleProvider):
        kwargs["locate"] = locate_tailscale
    return cls(**kwargs)


# ── selection, persistence, migration ─────────────────────────────────────


@dataclass(frozen=True)
class TransportSelection:
    """Which provider is chosen and what it was told."""

    provider: str = DEFAULT_PROVIDER
    config: Mapping = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"provider": self.provider, "provider_config": dict(self.config)}


def migrate_provider_id(payload: Mapping | None, *, legacy_public_url: str = "") -> str:
    """Decide which provider an install was already using.

    A build that predates this seam persisted only an address.  A
    ``*.trycloudflare.com`` one was minted by Rainette, so it was the Quick
    Tunnel; any other non-empty address was something the user brought
    themselves, which is exactly what ``manual`` now describes.  An install with
    no address at all has never been configured, so it lands on the default.
    """
    provider = str((payload or {}).get("provider") or "").strip()
    if known_provider(provider):
        return provider
    if provider:
        # An id written by a newer build. Fall back rather than fail closed on a
        # transport that does not exist here.
        return DEFAULT_PROVIDER
    public = str(legacy_public_url or "").strip()
    if not public:
        return DEFAULT_PROVIDER
    return CLOUDFLARE_QUICK if _host_of(public).endswith(_QUICK_TUNNEL_HOST_SUFFIX) else MANUAL


def _selection_path(app_data_dir) -> Path:
    return Path(app_data_dir) / TRANSPORT_CONFIG_FILENAME


def read_selection(app_data_dir, *, legacy_public_url: str = "") -> TransportSelection:
    """Load the chosen provider, migrating an older install on the way.

    A missing or damaged file must never stop Rainette from starting, and must
    never silently change which transport is in use: both paths resolve through
    the same migration, so an unconfigured install always lands on
    ``cloudflare-quick``.
    """
    try:
        payload = json.loads(_selection_path(app_data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    config = payload.get("provider_config")
    return TransportSelection(
        provider=migrate_provider_id(payload, legacy_public_url=legacy_public_url),
        config=dict(config) if isinstance(config, dict) else {},
    )


def write_selection(app_data_dir, provider: str, config: Mapping | None = None) -> TransportSelection:
    """Persist the chosen provider atomically.

    Rejecting an unknown id here rather than at launch keeps the stored value
    something every later read can trust.
    """
    provider_id = str(provider or "").strip() or DEFAULT_PROVIDER
    if not known_provider(provider_id):
        raise ValueError(f"unknown transport provider: {provider}")
    selection = TransportSelection(provider=provider_id, config=dict(config or {}))
    path = _selection_path(app_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(selection.as_dict(), separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return selection


def is_managed_url(value: str, *, provider: str = "", config: Mapping | None = None) -> bool:
    """True for an address Rainette itself brought up and may clear again.

    The legacy Quick Tunnel check is kept as a floor no matter which provider is
    selected: a ``*.trycloudflare.com`` address is always one we minted, and it
    always dies with the process, so leaving it baked into pairing links is the
    exact failure this whole area exists to prevent.
    """
    if _host_of(value).endswith(_QUICK_TUNNEL_HOST_SUFFIX):
        return True
    if not known_provider(provider):
        return False
    cls = provider_class(provider)
    return cls(config=config).owns_url(value)
