"""In-app updater: version comparison, the GitHub check's tri-state, and the
download/verify gate. The end-to-end download+install+relaunch can only be
exercised once a real v* release exists, so these pin everything up to (and the
refusal past) that boundary."""

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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


def _json_response(body):
    response = mock.MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    response.__enter__.return_value = response
    return response


class CheckForUpdatesTests(unittest.TestCase):
    """Mirrors the tri-state the Android release check already uses: a missing
    release is a normal 'unavailable', distinct from a 'check_failed' error, so
    the UI can stay quiet for the former and offer a retry for the latter."""

    def test_newer_release_reports_update(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response({"tag_name": "v0.9.0", "html_url": "u", "body": "notes"})):
            result = main.check_for_updates("0.2.2")
        self.assertEqual(result["status"], "update")
        self.assertEqual(result["latest"], "0.9.0")

    def test_same_version_reports_current(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response({"tag_name": "v0.2.2"})):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "current")

    def test_no_release_yet_is_unavailable_not_an_error(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 404, "nf", {}, None)):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "unavailable")

    def test_server_and_network_errors_are_check_failed(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 503, "down", {}, None)):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")

    def test_unparseable_tag_is_check_failed(self):
        with mock.patch.object(main.urllib.request, "urlopen",
                               return_value=_json_response({"tag_name": "not-a-version"})):
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")

    def test_check_never_raises(self):
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=ValueError("boom")):
            # ValueError is not one of the handled types; the bare except must
            # still turn it into check_failed rather than propagate.
            self.assertEqual(main.check_for_updates("0.2.2")["status"], "check_failed")


class InstallerDownloadTests(unittest.TestCase):
    def test_matching_checksum_writes_the_installer(self):
        payload = b"pretend installer bytes"
        digest = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(main, "_fetch_bytes", side_effect=[f"{digest}  RainetteMusicSetup.exe".encode(), payload]):
            with tempfile.TemporaryDirectory() as tmp:
                path = main._download_verified_installer(Path(tmp))
                self.assertEqual(path.read_bytes(), payload)

    def test_mismatched_checksum_aborts_before_writing(self):
        with mock.patch.object(main, "_fetch_bytes", side_effect=[f"{'0' * 64}  x".encode(), b"tampered"]):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError) as ctx:
                    main._download_verified_installer(Path(tmp))
                self.assertIn("checksum", str(ctx.exception))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_malformed_checksum_file_is_rejected(self):
        with mock.patch.object(main, "_fetch_bytes", side_effect=[b"", b"data"]):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    main._download_verified_installer(Path(tmp))


class ApplyUpdateGuardTests(unittest.TestCase):
    def test_apply_update_refuses_in_a_source_run(self):
        # A source checkout has no installer to swap in, so it must say so rather
        # than download an installer that can't replace anything.
        api = main.WindowApi()
        with mock.patch.object(main.sys, "frozen", False, create=True):
            result = api.apply_update()
        self.assertEqual(result["status"], "unsupported")

    def test_app_version_reports_the_constant(self):
        self.assertEqual(main.WindowApi().app_version(), version.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
