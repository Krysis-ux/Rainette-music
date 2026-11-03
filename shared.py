"""Runtime context shared by the music command handlers. ``server.py`` calls :func:`configure` once
at boot, before any handler runs."""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path
from typing import Any, Callable


def _ensure_trust_roots() -> str:
    """Point this process's TLS clients at the certificate bundle we ship."""
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
    """A verifying TLS client context that trusts the bundle chosen above."""
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
