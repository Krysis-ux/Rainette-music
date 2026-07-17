"""In-app updater: version comparison, the GitHub check's tri-state, and the
download/verify gate. The end-to-end download+install+relaunch can only be
exercised once a real v* release exists, so these pin everything up to (and the
refusal past) that boundary.

The root of trust is an Ed25519 signature over the release manifest's raw
bytes, verified against the public key committed in release_identity.py.
Authenticode is an optional second layer, enforced only when a certificate
fingerprint is pinned."""

import base64
import hashlib
import io
import json
import copy
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import main
import version


class VersionComparisonTests(unittest.TestCase):
    def test_parse_tolerates_v_prefix_and_suffixes(self):
        self.assertEqual(version.parse_version("v0.3.1"), (0, 3, 1))
        self.assertEqual(version.parse_version("0.2.2-local"), (0, 2, 2))
        self.assertEqual(version.parse_version("1.4.0+build.7"), (1, 4, 0))

    def test_unparseable_version_sorts_below_everything(self):
        self.assertEqual(version.parse_version("nightly"), ())
        self.assertFalse(version.is_newer("nightly", "0.0.1"))

    def test_normalize_strips_to_the_numeric_core(self):
        self.assertEqual(version.normalize("v1.4.0-beta+build"), "1.4.0")

    def test_is_newer_is_strict(self):
        self.assertTrue(version.is_newer("v0.3.0", "0.2.2"))
        self.assertFalse(version.is_newer("v0.2.2", "0.2.2"))
        self.assertFalse(version.is_newer("v0.2.1", "0.2.2"))
        # A ten-point jump in a later field still ranks correctly.
        self.assertTrue(version.is_newer("0.2.10", "0.2.9"))


INSTALLER_NAME = "RainetteMusicSetup.exe"
CHECKSUM_NAME = f"{INSTALLER_NAME}.sha256"
MANIFEST_NAME = "windows-release.json"
SIGNATURE_NAME = f"{MANIFEST_NAME}.sig"
# The release keypair every fixture signs with, plus an unrelated key for the
# attacker-holds-a-different-key cases. Generated fresh per test run: nothing
# here depends on a specific key, only on the pin matching the signer.
TEST_SIGNING_KEY = Ed25519PrivateKey.generate()
TEST_PUBLIC_KEY_B64 = base64.b64encode(TEST_SIGNING_KEY.public_key().public_bytes_raw()).decode()
OTHER_SIGNING_KEY = Ed25519PrivateKey.generate()
OTHER_PUBLIC_KEY_B64 = base64.b64encode(OTHER_SIGNING_KEY.public_key().public_bytes_raw()).decode()
# Only a certificate-holding release build pins an Authenticode fingerprint;
# the certless default leaves it empty and skips that layer entirely.
RAINETTE_SIGNER_SHA256 = "a" * 64
RELEASES_URL = "https://api.github.com/repos/Krysis-ux/Rainette-music/releases?per_page=20"
RELEASE_API_BASE = "https://api.github.com/repos/Krysis-ux/Rainette-music/releases"
ASSET_API_BASE = f"{RELEASE_API_BASE}/assets"


class _Response:
    def __init__(self, body: bytes, url: str = RELEASES_URL, headers=None):
        self._body = io.BytesIO(body)
        self._url = url
        self.headers = headers or {}
        self.status = 200

    def read(self, size=-1):
        return self._body.read(size)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _json_response(body, url: str = RELEASES_URL):
    return _Response(json.dumps(body).encode("utf-8"), url)


def _pinned_public_key(value: str = TEST_PUBLIC_KEY_B64):
    return mock.patch.object(main.release_identity, "UPDATE_SIGNER_PUBLIC_KEY", value)


def _pinned_signer(fingerprint: str = RAINETTE_SIGNER_SHA256):
    return mock.patch.object(main.release_identity, "UPDATE_SIGNER_CERT_SHA256", fingerprint)


def _frozen(flag: bool = True):
    return mock.patch.object(main.sys, "frozen", flag, create=True)


class _SignedBuildTestCase(unittest.TestCase):
    """Base for tests that need the updater to behave as it does in a release
    build: frozen, with the Ed25519 release key pinned and (by default) no
    Authenticode certificate."""

    def setUp(self):
        for patcher in (_pinned_public_key(), _frozen(True)):
            patcher.start()
            self.addCleanup(patcher.stop)


def _asset(asset_id: int, name: str, content: bytes, *, state: str = "uploaded", size=None):
    content_type = {
        INSTALLER_NAME: "application/x-msdownload",
        CHECKSUM_NAME: "text/plain",
        MANIFEST_NAME: "application/json",
        SIGNATURE_NAME: "application/octet-stream",
    }.get(name, "application/octet-stream")
    return {
        "id": asset_id,
        "name": name,
        "label": None,
        "state": state,
        "content_type": content_type,
        "size": len(content) if size is None else size,
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "browser_download_url": f"https://example.invalid/{name}",
    }


def _release_fixture(version_number="0.9.0", *, release_id=900, draft=False, prerelease=False,
                     channel="release", authenticode_signed=False, mutate_manifest=None,
                     signing_key=TEST_SIGNING_KEY):
    installer = b"MZ pretend signed Rainette installer"
    installer_hash = hashlib.sha256(installer).hexdigest()
    checksum = f"{installer_hash}  {INSTALLER_NAME}\n".encode()
    manifest_dict = {
        "schema": 2,
        "version": version_number,
        "channel": channel,
        "artifact": INSTALLER_NAME,
        "sha256": installer_hash,
        "authenticode": {
            "signed": authenticode_signed,
            "signerCertificateSha256": RAINETTE_SIGNER_SHA256 if authenticode_signed else "",
        },
    }
    if mutate_manifest is not None:
        mutate_manifest(manifest_dict)
    manifest = json.dumps(manifest_dict).encode()
    signature = base64.b64encode(signing_key.sign(manifest))
    assets = [
        _asset(901, INSTALLER_NAME, installer),
        _asset(902, CHECKSUM_NAME, checksum),
        _asset(903, MANIFEST_NAME, manifest),
        _asset(906, SIGNATURE_NAME, signature),
        _asset(904, "rainette-music-android.apk", b"android"),
        _asset(905, "DefinitelyNotRainette.exe", b"unrelated executable"),
    ]
    release = {
        "id": release_id,
        "tag_name": f"v{version_number}",
        "target_commitish": "main",
        "name": f"Rainette {version_number}",
        "body": "Release notes",
        "draft": draft,
        "prerelease": prerelease,
        "immutable": True,
        "published_at": "2026-07-15T00:00:00Z",
        "html_url": f"https://github.com/Krysis-ux/Rainette-music/releases/tag/v{version_number}",
        "assets": assets,
    }
    return release, {
        901: installer,
        902: checksum,
        903: manifest,
        906: signature,
    }


class _UrlRouter:
    def __init__(self, *, releases=None, releases_by_id=None, assets=None):
        self.releases = releases
        self.releases_by_id = releases_by_id or {}
        self.assets = assets or {}
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        self.requests.append((url, dict(request.header_items()), timeout))
        if url == RELEASES_URL and self.releases is not None:
            return _json_response(self.releases, url)
        if url.startswith(RELEASE_API_BASE + "/") and "/assets/" not in url:
            release_id = int(url.rsplit("/", 1)[-1])
            if release_id in self.releases_by_id:
                return _json_response(self.releases_by_id[release_id], url)
        if url.startswith(ASSET_API_BASE + "/"):
            asset_id = int(url.rsplit("/", 1)[-1])
            if asset_id in self.assets:
                content = self.assets[asset_id]
                return _Response(content, url, {"Content-Length": str(len(content))})
        raise AssertionError(f"unexpected updater URL: {url}")


class CheckForUpdatesTests(_SignedBuildTestCase):
    """Only a strict, complete Windows release may make the badge appear."""

    def test_newer_release_reports_update(self):
        release, _ = _release_fixture()
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response([release])):
            result = main.check_for_updates("0.2.2")
        self.assertEqual(result["status"], "update")
        self.assertEqual(result["latest"], "0.9.0")
        self.assertEqual(result["release_id"], 900)
        self.assertRegex(result["candidate_id"], r"^[0-9a-f]{64}$")

    def test_same_version_reports_current(self):
        release, _ = _release_fixture("0.2.2")
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response([release])):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "current")

    def test_no_releases_is_unavailable_not_an_error(self):
        with mock.patch.object(main.urllib.request, "urlopen", return_value=_json_response([])):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "unavailable")

    def test_missing_releases_endpoint_is_unavailable_not_an_error(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 404, "nf", {}, None)):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "unavailable")

    def test_server_and_network_errors_are_check_failed(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 503, "down", {}, None)):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")

    def test_live_non_semver_rainette_release_is_not_an_update(self):
        release, _ = _release_fixture()
        release["tag_name"] = "Rainette"
        release["assets"] = [
            _asset(476097444, INSTALLER_NAME, b"published Windows installer"),
            _asset(476097564, "rainette-music-android.apk", b"published Android installer"),
        ]
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response([release])):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "current")

    def test_rejects_incomplete_ambiguous_or_unpublished_windows_candidates(self):
        base, _ = _release_fixture()
        cases = {}

        for missing in (INSTALLER_NAME, CHECKSUM_NAME, MANIFEST_NAME, SIGNATURE_NAME):
            candidate = copy.deepcopy(base)
            candidate["assets"] = [asset for asset in candidate["assets"] if asset["name"] != missing]
            cases[f"missing {missing}"] = candidate

        duplicate = copy.deepcopy(base)
        duplicate["assets"].append(copy.deepcopy(duplicate["assets"][0]))
        duplicate["assets"][-1]["id"] = 999
        cases["duplicate installer"] = duplicate

        wrong_state = copy.deepcopy(base)
        wrong_state["assets"][0]["state"] = "new"
        cases["installer not uploaded"] = wrong_state

        arbitrary_only = copy.deepcopy(base)
        arbitrary_only["assets"] = [
            asset for asset in arbitrary_only["assets"] if asset["name"] != INSTALLER_NAME
        ]
        arbitrary_only["assets"].append(_asset(999, "OtherSoftware.exe", b"not Rainette"))
        cases["arbitrary exe"] = arbitrary_only

        invalid_digest = copy.deepcopy(base)
        invalid_digest["assets"][0]["digest"] = "sha256:not-a-digest"
        cases["invalid GitHub digest"] = invalid_digest

        oversized = copy.deepcopy(base)
        oversized["assets"][0]["size"] = 600 * 1024 * 1024
        cases["oversized installer"] = oversized

        draft = copy.deepcopy(base)
        draft["draft"] = True
        cases["draft"] = draft

        prerelease = copy.deepcopy(base)
        prerelease["prerelease"] = True
        cases["prerelease"] = prerelease

        unpublished = copy.deepcopy(base)
        unpublished["published_at"] = None
        cases["unpublished"] = unpublished

        for label, candidate in cases.items():
            with self.subTest(label=label):
                with mock.patch.object(main.urllib.request, "urlopen",
                                       return_value=_json_response([candidate])):
                    self.assertEqual(main.check_for_updates("0.2.2")["status"], "current")

    def test_selects_highest_valid_windows_version_not_an_unrelated_latest_release(self):
        older, _ = _release_fixture("0.5.0", release_id=500)
        higher, _ = _release_fixture("0.9.0", release_id=900)
        unrelated = copy.deepcopy(higher)
        unrelated["id"] = 1000
        unrelated["tag_name"] = "v1.0.0"
        unrelated["assets"] = [_asset(1001, "rainette-music-android.apk", b"android")]
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response([unrelated, older, higher])):
            result = main.check_for_updates("0.2.2")
        self.assertEqual(result["latest"], "0.9.0")
        self.assertEqual(result["release_id"], 900)

    def test_check_never_raises(self):
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=ValueError("boom")):
            # ValueError is not one of the handled types; the bare except must
            # still turn it into check_failed rather than propagate.
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")

    def test_accepts_the_content_types_github_actually_assigns_on_upload(self):
        # Regression: the real v0.2.3 release was rejected because GitHub's
        # upload path (action-gh-release) tags .exe as x-msdos-program and .sig
        # as pgp-signature, neither of which was on the allow-list. These are
        # the verbatim content types observed on the live published release.
        release, _ = _release_fixture()
        live_types = {
            INSTALLER_NAME: "application/x-msdos-program",
            CHECKSUM_NAME: "application/octet-stream",
            MANIFEST_NAME: "application/json",
            SIGNATURE_NAME: "application/pgp-signature",
        }
        for asset in release["assets"]:
            if asset["name"] in live_types:
                asset["content_type"] = live_types[asset["name"]]
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response([release])):
            result = main.check_for_updates("0.2.2")
        self.assertEqual(result["status"], "update")
        self.assertEqual(result["latest"], "0.9.0")


class ManifestSignatureTests(unittest.TestCase):
    """The Ed25519 manifest signature is the updater's root of trust."""

    def setUp(self):
        patcher = _pinned_public_key()
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _sig(manifest: bytes, key=TEST_SIGNING_KEY) -> bytes:
        return base64.b64encode(key.sign(manifest))

    def test_valid_signature_round_trips(self):
        manifest = b'{"schema": 2, "version": "0.9.0"}'
        main._verify_manifest_signature(manifest, self._sig(manifest))

    def test_tampered_manifest_is_rejected(self):
        manifest = b'{"schema": 2, "version": "0.9.0"}'
        signature = self._sig(manifest)
        tampered = manifest.replace(b"0.9.0", b"9.9.9")
        with self.assertRaisesRegex(RuntimeError, "not from Rainette's release signing key"):
            main._verify_manifest_signature(tampered, signature)

    def test_signature_from_a_different_key_is_rejected(self):
        manifest = b'{"schema": 2}'
        with self.assertRaisesRegex(RuntimeError, "not from Rainette's release signing key"):
            main._verify_manifest_signature(manifest, self._sig(manifest, OTHER_SIGNING_KEY))

    def test_malformed_signatures_are_rejected(self):
        manifest = b'{"schema": 2}'
        for label, signature in (
            ("not base64", b"!!! definitely not base64 !!!"),
            ("empty", b""),
            ("wrong length", base64.b64encode(b"short")),
            ("non-ascii", "signaturé".encode("utf-8")),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    main._verify_manifest_signature(manifest, signature)

    def test_rotation_accepts_a_signature_from_any_listed_key(self):
        manifest = b'{"schema": 2}'
        with _pinned_public_key(f"{OTHER_PUBLIC_KEY_B64}, {TEST_PUBLIC_KEY_B64}"):
            main._verify_manifest_signature(manifest, self._sig(manifest, TEST_SIGNING_KEY))
            main._verify_manifest_signature(manifest, self._sig(manifest, OTHER_SIGNING_KEY))

    def test_unconfigured_key_fails_closed(self):
        manifest = b'{"schema": 2}'
        with _pinned_public_key(""):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                main._verify_manifest_signature(manifest, self._sig(manifest))


class SourceRunAndKeylessUpdateTests(unittest.TestCase):
    """A build that could not install an update must not offer one.

    A source run has no installer to swap in, and a build whose committed
    release key is missing or corrupt could only refuse whatever it downloads.
    Either way the check must fail closed before it ever contacts GitHub."""

    def test_source_run_offers_no_update_and_never_asks_github(self):
        with _pinned_public_key(), _frozen(False), \
             mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=AssertionError("a source run must not ask GitHub")):
            result = main.check_for_updates("0.2.2")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["msg"], main.SOURCE_RUN_UPDATE_MSG)
        # No candidate may be named: the badge and install button key off these.
        self.assertNotIn("candidate_id", result)
        self.assertNotIn("latest", result)

    def test_missing_or_invalid_release_keys_all_fail_closed(self):
        cases = {
            "empty": "",
            "whitespace": "   ",
            "comma only": ",",
            "not base64": "!!!not-base64!!!",
            "wrong length": base64.b64encode(b"too short").decode(),
            "one bad entry in a rotation pair": f"{TEST_PUBLIC_KEY_B64},!!!not-base64!!!",
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                with _frozen(True), _pinned_public_key(value), \
                     mock.patch.object(main.urllib.request, "urlopen",
                                       side_effect=AssertionError("a keyless build must not ask GitHub")):
                    result = main.check_for_updates("0.2.2")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["msg"], main.UNCONFIGURED_KEY_UPDATE_MSG)

    def test_source_run_pins_no_candidate_so_apply_has_nothing_to_install(self):
        api = main.WindowApi()
        with _pinned_public_key(), _frozen(False), \
             mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=AssertionError("must not ask GitHub")):
            checked = api.check_for_updates()
        self.assertEqual(checked["status"], "unavailable")
        with _frozen(True), \
             mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=AssertionError("an unoffered update must not reach GitHub")):
            self.assertEqual(api.apply_update("")["status"], "no_update")

    def test_pinning_a_key_restores_the_offer(self):
        # The gate must track the key, not disable updates outright: the release
        # build has to keep offering eligible releases.
        release, _ = _release_fixture()
        with _pinned_public_key(), _frozen(True), \
             mock.patch.object(main.urllib.request, "urlopen", return_value=_json_response([release])):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "update")


class InstallerDownloadTests(_SignedBuildTestCase):
    def test_downloads_only_the_pinned_assets_and_streams_verified_installer(self):
        release, contents = _release_fixture()
        candidate = main._candidate_from_release(release)
        self.assertIsNotNone(candidate)
        router = _UrlRouter(assets=contents)
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
            with tempfile.TemporaryDirectory() as tmp:
                path = main._download_verified_installer(candidate, Path(tmp))
                self.assertEqual(path.read_bytes(), contents[901])
        # Manifest and signature come first: nothing else is even fetched until
        # the manifest's Ed25519 signature has verified.
        self.assertEqual(
            [request[0] for request in router.requests],
            [f"{ASSET_API_BASE}/903", f"{ASSET_API_BASE}/906",
             f"{ASSET_API_BASE}/902", f"{ASSET_API_BASE}/901"],
        )

    def test_wrong_key_signature_stops_the_download_before_any_installer_bytes(self):
        release, contents = _release_fixture(signing_key=OTHER_SIGNING_KEY)
        candidate = main._candidate_from_release(release)
        router = _UrlRouter(assets=contents)
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(RuntimeError, "not from Rainette's release signing key"):
                    main._download_verified_installer(candidate, Path(tmp))
                self.assertEqual(list(Path(tmp).iterdir()), [])
        # The installer asset (901) must never have been requested.
        requested = [request[0] for request in router.requests]
        self.assertNotIn(f"{ASSET_API_BASE}/901", requested)

    def test_signed_but_ineligible_manifest_is_rejected(self):
        # Defense in depth for validly-signed but wrong manifests — the replay
        # of a signed local-test build being the case that matters most.
        for label, mutate in (
            ("unsupported schema", lambda m: m.__setitem__("schema", 1)),
            ("local-test channel", lambda m: m.__setitem__("channel", "local-test")),
            ("version skew", lambda m: m.__setitem__("version", "9.9.9")),
            ("wrong artifact", lambda m: m.__setitem__("artifact", "OtherSoftware.exe")),
            ("hash mismatch", lambda m: m.__setitem__("sha256", "0" * 64)),
        ):
            with self.subTest(label=label):
                release, contents = _release_fixture(mutate_manifest=mutate)
                candidate = main._candidate_from_release(release)
                router = _UrlRouter(assets=contents)
                with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
                    with tempfile.TemporaryDirectory() as tmp:
                        with self.assertRaises(RuntimeError):
                            main._download_verified_installer(candidate, Path(tmp))
                        self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_sidecar_must_name_the_exact_installer_and_match_github_digest(self):
        for label, checksum in (
            ("wrong filename", f"{'0' * 64}  OtherSoftware.exe\n".encode()),
            ("wrong hash", f"{'0' * 64}  {INSTALLER_NAME}\n".encode()),
        ):
            with self.subTest(label=label):
                release, contents = _release_fixture()
                release["assets"][1] = _asset(902, CHECKSUM_NAME, checksum)
                contents[902] = checksum
                candidate = main._candidate_from_release(release)
                router = _UrlRouter(assets=contents)
                with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
                    with tempfile.TemporaryDirectory() as tmp:
                        with self.assertRaises(RuntimeError):
                            main._download_verified_installer(candidate, Path(tmp))
                        self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_truncated_or_digest_mismatched_installer_leaves_no_executable(self):
        for label, mutate_release, mutate_content in (
            ("truncated", lambda release: release["assets"][0].__setitem__("size", release["assets"][0]["size"] + 1), lambda data: data),
            ("digest mismatch", lambda _release: None, lambda data: data[:-1] + bytes([data[-1] ^ 1])),
        ):
            with self.subTest(label=label):
                release, contents = _release_fixture()
                mutate_release(release)
                contents[901] = mutate_content(contents[901])
                candidate = main._candidate_from_release(release)
                router = _UrlRouter(assets=contents)
                with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
                    with tempfile.TemporaryDirectory() as tmp:
                        with self.assertRaises(RuntimeError):
                            main._download_verified_installer(candidate, Path(tmp))
                        self.assertFalse((Path(tmp) / INSTALLER_NAME).exists())
                        self.assertFalse((Path(tmp) / f"{INSTALLER_NAME}.part").exists())

    def test_download_reports_byte_progress(self):
        release, contents = _release_fixture()
        candidate = main._candidate_from_release(release)
        seen = []
        router = _UrlRouter(assets=contents)
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
            with tempfile.TemporaryDirectory() as tmp:
                main._download_verified_installer(candidate, Path(tmp),
                                                  progress=lambda received, total: seen.append((received, total)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (candidate.installer.size, candidate.installer.size))


class ApplyUpdateGuardTests(_SignedBuildTestCase):
    def _join_worker(self, api):
        worker = api._update_worker
        self.assertIsNotNone(worker, "apply_update must have started the install worker")
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "install worker must finish")

    def test_authenticode_verification_fails_closed_when_windows_trust_is_unavailable(self):
        with mock.patch.object(main.os, "name", "posix"):
            with self.assertRaises(RuntimeError):
                main._verify_authenticode(Path("RainetteMusicSetup.exe"))

    def test_authenticode_verification_fails_closed_without_a_rainette_signer_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / INSTALLER_NAME
            installer.write_bytes(b"MZ signed installer fixture")
            with mock.patch.object(main.os, "name", "nt"), \
                 mock.patch.object(main.release_identity, "UPDATE_SIGNER_CERT_SHA256", ""), \
                 mock.patch.object(main, "_verify_windows_authenticode_trust") as trust, \
                 mock.patch.object(main, "_authenticode_signer_sha256") as signer:
                with self.assertRaisesRegex(RuntimeError, "identity is not configured"):
                    main._verify_authenticode(installer)
        trust.assert_not_called()
        signer.assert_not_called()

    def test_authenticode_verification_rejects_a_windows_trusted_but_wrong_signer(self):
        trusted_rainette_signer = "a" * 64
        other_windows_trusted_signer = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / INSTALLER_NAME
            installer.write_bytes(b"MZ signed installer fixture")
            with mock.patch.object(main.os, "name", "nt"), \
                 mock.patch.object(
                     main.release_identity,
                     "UPDATE_SIGNER_CERT_SHA256",
                     trusted_rainette_signer,
                 ), \
                 mock.patch.object(main, "_verify_windows_authenticode_trust") as trust, \
                 mock.patch.object(
                     main,
                     "_authenticode_signer_sha256",
                     return_value=other_windows_trusted_signer,
                 ) as signer:
                with self.assertRaisesRegex(RuntimeError, "not Rainette's trusted release identity"):
                    main._verify_authenticode(installer)
        trust.assert_called_once_with(installer)
        signer.assert_called_once_with(installer)

    def test_authenticode_verification_requires_os_trust_and_an_allowlisted_signer(self):
        rainette_signer = "A" * 64
        rollover_signer = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / INSTALLER_NAME
            installer.write_bytes(b"MZ signed installer fixture")
            with mock.patch.object(main.os, "name", "nt"), \
                 mock.patch.object(
                     main.release_identity,
                     "UPDATE_SIGNER_CERT_SHA256",
                     f"{rainette_signer}, {rollover_signer}",
                 ), \
                 mock.patch.object(main, "_verify_windows_authenticode_trust") as trust, \
                 mock.patch.object(
                     main,
                     "_authenticode_signer_sha256",
                     return_value=rainette_signer.lower(),
                 ) as signer:
                main._verify_authenticode(installer)
        trust.assert_called_once_with(installer)
        signer.assert_called_once_with(installer)

    def test_check_pins_candidate_and_apply_requires_that_candidate_id(self):
        release, _ = _release_fixture()
        api = main.WindowApi()
        router = _UrlRouter(releases=[release])
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
            checked = api.check_for_updates()
        self.assertEqual(checked["status"], "update")
        for untrusted_id in ("", "0" * 64):
            with self.subTest(candidate_id=untrusted_id or "omitted"), \
                 mock.patch.object(main.urllib.request, "urlopen",
                                   side_effect=AssertionError("stale ID must not reach GitHub")):
                result = api.apply_update(untrusted_id)
            self.assertEqual(result["status"], "stale")

    def test_apply_revalidates_the_exact_release_and_asset_ids_before_download(self):
        release, _ = _release_fixture()
        api = main.WindowApi()
        with mock.patch.object(
            main.urllib.request,
            "urlopen",
            side_effect=_UrlRouter(releases=[release]),
        ):
            checked = api.check_for_updates()

        replaced = copy.deepcopy(release)
        replaced["assets"][0]["id"] = 999
        router = _UrlRouter(releases_by_id={900: replaced})
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
            result = api.apply_update(checked["candidate_id"])

        self.assertEqual(result["status"], "stale")
        self.assertEqual([request[0] for request in router.requests], [f"{RELEASE_API_BASE}/900"])

    def test_bad_signature_surfaces_as_failed_progress_and_never_launches_or_closes_the_app(self):
        # apply_update() now returns "installing" optimistically; a signature
        # that fails to verify must surface through update_progress() while
        # leaving the running app untouched and the lock released for a retry.
        release, contents = _release_fixture(signing_key=OTHER_SIGNING_KEY)
        api = main.WindowApi()
        main_window = mock.Mock()
        player_window = mock.Mock()
        api.bind_main(main_window)
        api.bind_player(player_window)
        with mock.patch.object(
            main.urllib.request,
            "urlopen",
            side_effect=_UrlRouter(releases=[release]),
        ):
            checked = api.check_for_updates()

        router = _UrlRouter(releases_by_id={900: release}, assets=contents)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(main.urllib.request, "urlopen", side_effect=router), \
             mock.patch.object(main.tempfile, "mkdtemp", return_value=str(Path(tmp) / "update")), \
             mock.patch.object(main.subprocess, "Popen") as popen, \
             mock.patch.object(main.threading, "Timer") as timer:
            result = api.apply_update(checked["candidate_id"])
            self._join_worker(api)
            progress = api.update_progress()
            update_files_left_behind = (Path(tmp) / "update").exists()

        self.assertEqual(result["status"], "installing")
        self.assertEqual(progress["phase"], "failed")
        self.assertEqual(progress["code"], "verification_or_launch_failed")
        self.assertEqual(progress["message"], "Rainette could not verify or start the update. Please try again.")
        self.assertFalse(update_files_left_behind)
        popen.assert_not_called()
        timer.assert_not_called()
        main_window.destroy.assert_not_called()
        player_window.destroy.assert_not_called()
        # The failure released the lock: a retry is not spuriously "busy".
        self.assertTrue(api._update_apply_lock.acquire(blocking=False))
        api._update_apply_lock.release()

    def test_pinned_cert_requires_authenticode_and_its_failure_never_launches(self):
        release, contents = _release_fixture(authenticode_signed=True)
        api = main.WindowApi()
        with mock.patch.object(
            main.urllib.request,
            "urlopen",
            side_effect=_UrlRouter(releases=[release]),
        ):
            checked = api.check_for_updates()

        router = _UrlRouter(releases_by_id={900: release}, assets=contents)
        with tempfile.TemporaryDirectory() as tmp, \
             _pinned_signer(), \
             mock.patch.object(main.urllib.request, "urlopen", side_effect=router), \
             mock.patch.object(main.tempfile, "mkdtemp", return_value=str(Path(tmp) / "update")), \
             mock.patch.object(main, "_verify_authenticode",
                               side_effect=RuntimeError("installer signature is not trusted")) as verify, \
             mock.patch.object(main.subprocess, "Popen") as popen, \
             mock.patch.object(main.threading, "Timer") as timer:
            api.apply_update(checked["candidate_id"])
            self._join_worker(api)
            progress = api.update_progress()

        self.assertEqual(progress["phase"], "failed")
        verify.assert_called_once()
        popen.assert_not_called()
        timer.assert_not_called()

    def test_pinned_cert_rejects_a_manifest_that_does_not_promise_authenticode(self):
        # A certless manifest replayed against a cert-pinned build must refuse
        # before Authenticode runs: the manifest itself has to promise a signed
        # installer.
        release, contents = _release_fixture(authenticode_signed=False)
        candidate = main._candidate_from_release(release)
        router = _UrlRouter(assets=contents)
        with _pinned_signer(), \
             mock.patch.object(main.urllib.request, "urlopen", side_effect=router):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(RuntimeError, "Authenticode"):
                    main._download_verified_installer(candidate, Path(tmp))

    def test_successful_apply_uses_only_pinned_assets_then_starts_shutdown(self):
        release, contents = _release_fixture()
        api = main.WindowApi()
        with mock.patch.object(
            main.urllib.request,
            "urlopen",
            side_effect=_UrlRouter(releases=[release]),
        ):
            checked = api.check_for_updates()

        router = _UrlRouter(releases_by_id={900: release}, assets=contents)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(main.urllib.request, "urlopen", side_effect=router), \
             mock.patch.object(main.tempfile, "mkdtemp", return_value=str(Path(tmp) / "update")), \
             mock.patch.object(main, "_verify_authenticode") as verify, \
             mock.patch.object(main.subprocess, "Popen") as popen, \
             mock.patch.object(main.threading, "Timer") as timer:
            result = api.apply_update(checked["candidate_id"])
            self._join_worker(api)
            progress = api.update_progress()
            repeated = api.apply_update(checked["candidate_id"])
            launched_installer = Path(popen.call_args.args[0][0])
            self.assertTrue(launched_installer.is_file())

        self.assertEqual(result, {"status": "installing", "version": "0.9.0"})
        self.assertEqual(progress, {"phase": "installing", "version": "0.9.0"})
        # Success keeps the apply lock held until the process exits, so a second
        # click can never re-launch the verified installer.
        self.assertEqual(repeated["status"], "busy")
        # No Authenticode certificate is pinned in this build, so the optional
        # layer must not run — the Ed25519 manifest signature is the gate.
        verify.assert_not_called()
        self.assertEqual(
            popen.call_args.args[0],
            [str(launched_installer), "/VERYSILENT", "/NORESTART", "/autorelaunch=1"],
        )
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        timer.return_value.start.assert_called_once_with()
        self.assertEqual(
            [request[0] for request in router.requests],
            [
                f"{RELEASE_API_BASE}/900",
                f"{ASSET_API_BASE}/903",
                f"{ASSET_API_BASE}/906",
                f"{ASSET_API_BASE}/902",
                f"{ASSET_API_BASE}/901",
            ],
        )

    def test_installer_launch_failure_cleans_up_without_closing_app(self):
        release, contents = _release_fixture()
        api = main.WindowApi()
        main_window = mock.Mock()
        player_window = mock.Mock()
        api.bind_main(main_window)
        api.bind_player(player_window)
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=_UrlRouter(releases=[release])):
            checked = api.check_for_updates()

        router = _UrlRouter(releases_by_id={900: release}, assets=contents)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(main.urllib.request, "urlopen", side_effect=router), \
             mock.patch.object(main.tempfile, "mkdtemp", return_value=str(Path(tmp) / "update")), \
             mock.patch.object(main.subprocess, "Popen", side_effect=OSError("launch failed")), \
             mock.patch.object(main.threading, "Timer") as timer:
            api.apply_update(checked["candidate_id"])
            self._join_worker(api)
            progress = api.update_progress()
            update_files_left_behind = (Path(tmp) / "update").exists()

        self.assertEqual(progress["phase"], "failed")
        self.assertEqual(progress["code"], "verification_or_launch_failed")
        self.assertNotIn("launch failed", progress["message"])
        self.assertFalse(update_files_left_behind)
        timer.assert_not_called()
        main_window.destroy.assert_not_called()
        player_window.destroy.assert_not_called()

    def test_concurrent_apply_is_rejected_without_closing_app(self):
        release, _ = _release_fixture()
        api = main.WindowApi()
        main_window = mock.Mock()
        player_window = mock.Mock()
        api.bind_main(main_window)
        api.bind_player(player_window)
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=_UrlRouter(releases=[release])):
            checked = api.check_for_updates()

        entered = threading.Event()
        release_download = threading.Event()

        def blocking_download(_candidate, destination, progress=None):
            entered.set()
            self.assertTrue(release_download.wait(timeout=5))
            return destination / INSTALLER_NAME

        router = _UrlRouter(releases_by_id={900: release})
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(main.urllib.request, "urlopen", side_effect=router), \
             mock.patch.object(main.tempfile, "mkdtemp", return_value=str(Path(tmp) / "update")), \
             mock.patch.object(main, "_download_verified_installer", side_effect=blocking_download), \
             mock.patch.object(main.subprocess, "Popen"), \
             mock.patch.object(main.threading, "Timer"):
            first = api.apply_update(checked["candidate_id"])
            self.assertTrue(entered.wait(timeout=5))
            concurrent = api.apply_update(checked["candidate_id"])
            release_download.set()
            self._join_worker(api)

        self.assertEqual(first["status"], "installing")
        self.assertEqual(concurrent["status"], "busy")
        main_window.destroy.assert_not_called()
        player_window.destroy.assert_not_called()

    def test_apply_without_a_successful_check_never_contacts_github(self):
        api = main.WindowApi()
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=AssertionError("must not contact GitHub")):
            result = api.apply_update("0" * 64)
        self.assertEqual(result["status"], "no_update")

    def test_apply_update_refuses_in_a_source_run(self):
        # A source checkout has no installer to swap in, so it must say so rather
        # than download an installer that can't replace anything.
        api = main.WindowApi()
        with _frozen(False):
            result = api.apply_update()
        self.assertEqual(result["status"], "unsupported")

    def test_update_progress_starts_idle(self):
        self.assertEqual(main.WindowApi().update_progress(), {"phase": "idle"})

    def test_app_version_reports_the_constant(self):
        self.assertEqual(main.WindowApi().app_version(), version.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
