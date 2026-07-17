"""Generate the Ed25519 release-signing keypair for Rainette self-updates.

Run this ONCE on a trusted machine:

    python release/new_signing_key.py

Then:
  1. Paste the PUBLIC key into version.py (UPDATE_SIGNER_PUBLIC_KEY)
     and commit it. It is public — committing it is the point.
  2. Add the PRIVATE key as the GitHub Actions repository secret
     UPDATE_SIGNING_KEY (Settings > Secrets and variables > Actions).
  3. Store an offline backup of the private key (password manager, printed
     copy in a safe — anywhere that is not this repository).

Key custody is the whole security model:
  * LOSE the private key and you can never ship a self-update again — every
    user would have to download and reinstall by hand.
  * LEAK it and an attacker can sign malicious updates; rotating to a new key
    requires shipping an update signed by the OLD key first, so rotation after
    a leak is a race you do not want to run.
Back it up before doing anything else.
"""

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
