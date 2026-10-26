"""Runtime context shared by the music command handlers.

``server.py`` calls :func:`configure` once at boot, before any handler runs.
Handlers read these via module attributes at call time (late binding), which
provides stable module-level configuration for ``music_bridge``.
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path
from typing import Any, Callable


def _ensure_trust_roots() -> str:
    """Point this process's TLS clients at the certificate bundle we ship.

    A frozen build carries its own OpenSSL, and OpenSSL looks for CA
    certificates at a path compiled in **on the build machine**. Read out of a
    real macOS install, that is:

        /Library/Frameworks/Python.framework/Versions/3.12/etc/openssl/

    — the CI runner's python.org install, which exists on no user's computer.
    The result is a process with *zero* trust roots, where every TLS handshake
    fails with "unable to get local issuer certificate" before a byte leaves the
    machine. Nothing plays, nothing downloads, no tunnel probe succeeds, and the
    app blamed the user's network for all of it.

    Fixing it in yt-dlp alone is not enough: aiohttp (the audio relay),
    urllib (the tunnel probe and the updater) and ytmusicapi (search) each build
    their own context. `SSL_CERT_FILE` is the one lever they all read, because
    it feeds `SSLContext.set_default_verify_paths()`.

    Deliberately *not* a global `ssl.SSLContext` replacement: that is what an
    earlier truststore injection did, and it installed a client-only context
    that made the companion's TLS **server** accept connections and drop them.
    An environment variable changes where clients look for roots and nothing
    else.

    Returns the bundle path in use, or "" when the platform already has one.
    """
    # An operator who set this themselves knows better than we do -- but only
    # if it points at something real. A stale value naming a file that is gone
    # is the same "no roots" failure wearing a different hat.
    existing = os.environ.get("SSL_CERT_FILE", "")
    if existing and Path(existing).is_file():
        return existing
    try:
        if ssl.create_default_context().get_ca_certs():
            return ""          # this Python can already verify; leave it alone
    except Exception:
        pass
    try:
        import certifi
        bundle = Path(certifi.where())
    except Exception:
        return ""
    if not bundle.is_file():
        return ""
    os.environ["SSL_CERT_FILE"] = str(bundle)
    os.environ.setdefault("SSL_CERT_DIR", str(bundle.parent))
    return str(bundle)


# Run at import. Contexts created after this see the bundle; one created
# before it does not, and aiohttp creates one when it is imported -- see
# client_ssl_context().
TRUST_BUNDLE = _ensure_trust_roots()


def client_ssl_context() -> ssl.SSLContext:
    """A verifying TLS client context that trusts the bundle chosen above.

    aiohttp builds its default context once, when it is first imported, and
    server.py imports aiohttp before this module. In the frozen macOS build that
    cached context has no roots at all, so a session left on aiohttp's default
    could not open a single googlevideo URL: every phone stream and the EQ
    proxy failed with CERTIFICATE_VERIFY_FAILED while search, which builds its
    context later, kept working. Give every aiohttp session this instead.
    """
    context = ssl.create_default_context()
    if TRUST_BUNDLE:
        context.load_verify_locations(cafile=TRUST_BUNDLE)
    return context

STATE: Any = None
POLICY: Any = None

_notify: Callable[[dict], None] | None = None


def configure(*, state: Any, notify: Callable[[dict], None], policy: Any = None) -> None:
    """Wire the live runtime services in. Called once from server.py."""
    global STATE, POLICY, _notify
    STATE = state
    POLICY = policy
    _notify = notify


def notify_browsers(msg: dict) -> None:
    """Broadcast to connected browsers. No-op until configure() runs."""
    if _notify is not None:
        _notify(msg)
