"""Pinned release-signing identity for Rainette Music self-updates.

The Windows release build must replace the empty value below with the SHA-256
fingerprint of the leaf certificate used to Authenticode-sign Rainette's
installer.  A comma-separated list is accepted to support an intentional
certificate rollover.  Keeping the source default empty makes source and
misconfigured builds fail closed instead of trusting any Windows-valid signer.
"""

UPDATE_SIGNER_CERT_SHA256 = ""
