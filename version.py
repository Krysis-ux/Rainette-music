"""Single source of truth for the running app's version."""

from __future__ import annotations

import re

APP_VERSION = "0.6.0"

# Release-signing identity for self-updates. The PUBLIC Ed25519 key below can
# only verify signatures, never create them (the private key lives outside the
# repo); comma-separate multiple keys during an intentional rotation.
UPDATE_SIGNER_PUBLIC_KEY = "cC0YeVcuZx/JhAU4Ctg/jOEDoIO8nCb/J0bLJSuzUJk="

# Optional Authenticode layer, enforced only when a certificate fingerprint is
# injected by the release build. Required by the updater even while empty.
UPDATE_SIGNER_CERT_SHA256 = ""


def parse_version(value: str) -> tuple[int, ...]:
    """Turn a version/tag string into a comparable tuple of ints."""
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
