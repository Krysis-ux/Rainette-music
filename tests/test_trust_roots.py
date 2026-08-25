"""The frozen build shipped unable to verify a single certificate.

A PyInstaller bundle carries its own OpenSSL, and OpenSSL looks for CA
certificates at a path compiled in **on the build machine**. Read out of a real
macOS install of 0.4.2:

    /Library/Frameworks/Python.framework/Versions/3.12/etc/openssl/cert.pem

That is the CI runner's python.org install. It exists on no user's computer, so
the app ran with *zero* trust roots: every TLS handshake failed with "unable to
get local issuer certificate" before a byte left the machine. Nothing played,
nothing downloaded, no tunnel probe succeeded — and the app blamed the user's
network, including on their own home Wi-Fi.

Fixing yt-dlp alone would not have been enough. aiohttp (the audio relay),
urllib (the tunnel probe and updater) and ytmusicapi (search) each build their
own SSL context. `SSL_CERT_FILE` is the one lever all of them read.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_with_no_roots(script: str) -> subprocess.CompletedProcess:
    """Run *script* in a process whose OpenSSL can find no CA bundle."""
    env = {
        **os.environ,
        "SSL_CERT_FILE": "/nonexistent/cert.pem",
        "SSL_CERT_DIR": "/nonexistent/certs",
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env,
    )


def _can_simulate_no_roots() -> bool:
    """Can this platform be put into the broken state at all?

    On Windows it cannot: Python loads the system certificate store regardless
    of `SSL_CERT_FILE`, so the probe still sees hundreds of roots. That is not a
    gap in the test — it is the reason the bug is macOS-only, and why
    `no-certifi` is right on Windows and wrong everywhere else. The tests that
    depend on the simulation skip rather than assert something the platform
    makes impossible.
    """
    try:
        probe = _run_with_no_roots(
            "import ssl; print(len(ssl.create_default_context().get_ca_certs()))"
        )
        return probe.stdout.strip() == "0"
    except Exception:
        return False


NO_ROOTS_SIMULATABLE = _can_simulate_no_roots()
_NEEDS_SIMULATION = unittest.skipUnless(
    NO_ROOTS_SIMULATABLE,
    "this platform always has trust roots (Windows loads the system store "
    "regardless), which is why the bug it guards cannot happen here",
)


class TrustRootTests(unittest.TestCase):
    @_NEEDS_SIMULATION
    def test_a_process_with_no_roots_really_has_none(self):
        """The premise. Without it the tests below prove nothing."""
        probe = _run_with_no_roots(
            "import ssl; print(len(ssl.create_default_context().get_ca_certs()))"
        )
        self.assertEqual(probe.stdout.strip(), "0", probe.stderr[-400:])

    @_NEEDS_SIMULATION
    def test_importing_shared_restores_them(self):
        probe = _run_with_no_roots(
            "import shared, ssl; "
            "print(len(ssl.create_default_context().get_ca_certs()))"
        )
        self.assertTrue(probe.stdout.strip().isdigit(), probe.stderr[-600:])
        self.assertGreater(
            int(probe.stdout.strip()), 0,
            "importing shared must give the process trust roots; without them "
            "every TLS client in the app fails before reaching the network",
        )

    @_NEEDS_SIMULATION
    def test_a_stale_ssl_cert_file_is_not_trusted(self):
        """A variable naming a file that is gone is still "no roots"."""
        probe = _run_with_no_roots("import shared; print(bool(shared.TRUST_BUNDLE))")
        self.assertEqual(probe.stdout.strip(), "True", probe.stderr[-400:])

    def test_a_platform_that_already_works_is_left_alone(self):
        """Never override a working system store — that is Windows' whole case."""
        probe = subprocess.run(
            [sys.executable, "-c", "import shared; print(repr(shared.TRUST_BUNDLE))"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(probe.stdout.strip(), "''", probe.stderr[-400:])

    def test_the_ssl_context_class_is_not_replaced(self):
        """Swapping SSLContext globally broke the companion's TLS *server*.

        An earlier truststore injection installed a client-only context in the
        stdlib, and the gateway then accepted HTTPS connections and dropped
        them. Setting an environment variable must not repeat that.
        """
        probe = _run_with_no_roots(
            "import ssl; before = ssl.SSLContext; import shared; "
            "print(ssl.SSLContext is before)"
        )
        self.assertEqual(probe.stdout.strip(), "True", probe.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
