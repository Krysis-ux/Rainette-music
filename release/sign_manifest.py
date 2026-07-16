"""Sign windows-release.json with the Ed25519 release key.

Writes a detached signature (base64, over the manifest's raw bytes) to
<manifest>.sig — the asset the in-app updater verifies against the public key
committed in release_identity.py.

Deliberately tiny: this is the only code the credential-holding CI job runs,
preserving the release pipeline's isolation principle (the job with the secret
never executes arbitrary project code).

Usage:
    RAINETTE_UPDATE_SIGNING_KEY=<base64 raw Ed25519 private key> \
        python release/sign_manifest.py <path-to-windows-release.json>
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: sign_manifest.py <path-to-windows-release.json>\n")
        return 2
    manifest_path = Path(argv[1])
    if not manifest_path.is_file():
        sys.stderr.write(f"manifest not found: {manifest_path}\n")
        return 2

    encoded_key = os.environ.get("RAINETTE_UPDATE_SIGNING_KEY", "").strip()
    if not encoded_key:
        sys.stderr.write("RAINETTE_UPDATE_SIGNING_KEY is not set\n")
        return 2
    try:
        raw_key = base64.b64decode(encoded_key, validate=True)
    except Exception:
        sys.stderr.write("RAINETTE_UPDATE_SIGNING_KEY is not valid base64\n")
        return 2
    if len(raw_key) != 32:
        sys.stderr.write("RAINETTE_UPDATE_SIGNING_KEY must be a raw 32-byte Ed25519 private key\n")
        return 2

    manifest_bytes = manifest_path.read_bytes()
    signature = Ed25519PrivateKey.from_private_bytes(raw_key).sign(manifest_bytes)
    signature_path = manifest_path.with_name(manifest_path.name + ".sig")
    signature_path.write_bytes(base64.b64encode(signature))
    sys.stdout.write(f"Signed {manifest_path.name} -> {signature_path.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
