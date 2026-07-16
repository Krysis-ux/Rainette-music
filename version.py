"""Single source of truth for the running app's version.

Kept in sync with the build: ``build-windows-release.ps1 -Version`` and the
``v*`` git tag the release workflow fires on must match ``APP_VERSION`` (the
build script asserts this). The in-app updater compares this against the latest
GitHub release tag to decide whether an update is available.
"""

from __future__ import annotations

import re

APP_VERSION = "0.2.3"


def parse_version(value: str) -> tuple[int, ...]:
    """Turn a version/tag string into a comparable tuple of ints.

    Tolerant of a leading ``v`` and a prerelease/build suffix, so ``"v0.3.1"`` and
    ``"0.3.1-local"`` both parse to ``(0, 3, 1)``. Unparseable input yields ``()``,
    which compares lower than any real version.
    """
    if not value:
        return ()
    core = str(value).strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for chunk in core.split("."):
        match = re.match(r"\d+", chunk.strip())
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def normalize(value: str) -> str:
    """The comparable core of a version string (``"v0.3.1-beta"`` -> ``"0.3.1"``)."""
    return ".".join(str(n) for n in parse_version(value))


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    """True when ``candidate`` is a strictly newer version than ``current``."""
    candidate_parts = parse_version(candidate)
    if not candidate_parts:
        return False
    return candidate_parts > parse_version(current)
