"""Generate the Ed25519 release-signing keypair for Rainette self-updates."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    private_key = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(private_key.private_bytes_raw()).decode("ascii")
    public_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")

    # ASCII-only output: Windows consoles often decode cp1252 and turn fancy
    # dashes into mojibake right where the user is copying key material.
    print("Rainette release-signing keypair generated.\n")
    print("PUBLIC key - commit this into version.py:")
    print(f'    UPDATE_SIGNER_PUBLIC_KEY = "{public_b64}"\n')
    print("PRIVATE key - GitHub secret UPDATE_SIGNING_KEY (and an offline backup):")
    print(f"    {private_b64}\n")
    print("WARNING: this private key is shown only once and is not stored anywhere.")
    print("Lose it = no more self-updates, ever. Leak it = attackers can sign updates.")
    print("Back it up offline BEFORE closing this terminal.")


if __name__ == "__main__":
    main()
