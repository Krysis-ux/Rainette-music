"""Runtime context shared by the music command handlers.

``server.py`` calls :func:`configure` once at boot, before any handler runs.
Handlers read these via module attributes at call time (late binding), which
provides stable module-level configuration for ``music_bridge``.
"""

from __future__ import annotations

from typing import Any, Callable

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
