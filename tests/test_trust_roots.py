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

import datetime
import ipaddress
import os
import subprocess
import sys
import tempfile
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


def _write_test_chain(directory: Path) -> tuple[Path, Path, Path]:
    """A throwaway root and a 127.0.0.1 certificate it signed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    window = dict(not_valid_before=now - datetime.timedelta(minutes=5), not_valid_after=now + datetime.timedelta(days=1))
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rainette test root")])
    root = (
        x509.CertificateBuilder(subject_name=root_name, issuer_name=root_name, public_key=root_key.public_key(),
                                serial_number=x509.random_serial_number(), **window)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder(subject_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]),
                                issuer_name=root_name, public_key=leaf_key.public_key(),
                                serial_number=x509.random_serial_number(), **window)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    paths = directory / "root.pem", directory / "leaf.pem", directory / "leaf.key"
    paths[0].write_bytes(root.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
    return paths


# Imports in the order the app does: aiohttp first (server.py line order), then
# shared picks the shipped bundle -- here the test root, via certifi.where().
_RELAY_PROBE = """
import aiohttp, asyncio, http.server, ssl, threading
import certifi
certifi.where = lambda: {ca!r}
import server
from aiohttp import web

tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain({leaf!r}, {key!r})
class Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *args):
        pass
httpd = http.server.HTTPServer(("127.0.0.1", 0), Ok)
httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

async def main():
    app = web.Application()
    await server._on_startup(app)
    try:
        async with app[server.CLIENT_KEY].get("https://127.0.0.1:%d/" % httpd.server_port) as response:
            print(response.status)
    except Exception as exc:
        print(type(exc).__name__, exc)
    finally:
        await server._on_cleanup(app)

asyncio.run(main())
"""


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

    @_NEEDS_SIMULATION
    def test_the_audio_relay_trusts_the_bundle_even_though_aiohttp_loads_first(self):
        """Every phone stream died with CERTIFICATE_VERIFY_FAILED in 0.5.0.

        aiohttp builds its default context when it is imported, and server.py
        imports it before shared. In a process with no roots that context
        verifies nothing, and the relay used it: search worked, every stream
        failed. A local HTTPS server signed by a test root stands in for
        googlevideo, and that root stands in for the bundle the app ships.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ca, leaf, key = _write_test_chain(Path(tmp))
            probe = _run_with_no_roots(_RELAY_PROBE.format(ca=str(ca), leaf=str(leaf), key=str(key)))
        self.assertEqual(probe.stdout.strip().splitlines()[-1:], ["200"], probe.stdout[-400:] + probe.stderr[-800:])

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
