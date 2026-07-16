"""Pinned release-signing identity for Rainette Music self-updates.

UPDATE_SIGNER_PUBLIC_KEY is the root of trust: one or more base64-encoded raw
Ed25519 public keys, comma-separated to support an intentional key rotation.
It is committed in source on purpose — the public half needs no secrecy, and
committing it makes the trusted identity auditable in git history and identical
across every build. Release CI signs windows-release.json with the matching
private key (the UPDATE_SIGNING_KEY repository secret) and the app refuses any
update whose manifest signature does not verify against one of these keys.
Generate a keypair with release/new_signing_key.py; an empty value fails closed.

UPDATE_SIGNER_CERT_SHA256 is an optional second layer: the SHA-256 fingerprint
of the leaf certificate used to Authenticode-sign Rainette's installer. Kept
empty while Rainette has no code-signing certificate — Authenticode is then not
required. When a certificate exists, the release build injects the fingerprint
and the installer must also carry a Windows-trusted signature from it.
"""

UPDATE_SIGNER_PUBLIC_KEY = "cC0YeVcuZx/JhAU4Ctg/jOEDoIO8nCb/J0bLJSuzUJk="

UPDATE_SIGNER_CERT_SHA256 = ""
