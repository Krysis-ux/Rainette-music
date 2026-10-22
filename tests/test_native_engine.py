"""Contract between the Python engine (main.py --engine) and the native macOS app."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import main

ROOT = Path(__file__).resolve().parents[1]


def _call(base, token, method, args=()):
    request = urllib.request.Request(
        f"{base}/native/{method}", data=json.dumps({"args": list(args)}).encode(), method="POST",
        headers={"X-Rainette-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


# The engine runs under the native app on macOS only; HOME isolates its data there.
@unittest.skipUnless(sys.platform == "darwin", "the native app is macOS-only")
class EngineProcessTests(unittest.TestCase):
    def test_engine_handshakes_bridges_and_dies_with_its_front_end(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            engine = subprocess.Popen(
                [sys.executable, str(ROOT / "main.py"), "--engine"], cwd=ROOT, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                line = engine.stdout.readline()
                self.assertTrue(line.startswith(main.ENGINE_HANDSHAKE_PREFIX), line)
                handshake = json.loads(line[len(main.ENGINE_HANDSHAKE_PREFIX):])
                base = f"http://127.0.0.1:{handshake['port']}"
                token = handshake["token"]

                self.assertEqual(_call(base, token, "app_version"), (200, {"ok": True, "result": handshake["version"]}))
                self.assertEqual(_call(base, token, "reveal_player")[0], 404)
                self.assertEqual(_call(base, "not-the-token", "app_version")[0], 403)

                engine.stdin.close()
                self.assertEqual(engine.wait(timeout=15), 0)
            finally:
                if engine.poll() is None:
                    engine.kill()


class NativeDispatchTests(unittest.TestCase):
    def test_only_listed_methods_are_callable(self):
        dispatch = main._native_dispatcher(main.WindowApi())
        self.assertEqual(dispatch("app_version", []), main.version.APP_VERSION)
        for method in ("reveal_player", "player_hide", "_quit_for_update", "__init__"):
            with self.assertRaises(KeyError, msg=method):
                dispatch(method, [])

    def test_every_listed_method_exists(self):
        for method in main.NATIVE_API_METHODS:
            self.assertTrue(callable(getattr(main.WindowApi, method, None)), method)

    def test_a_folder_the_picker_returned_is_watched(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch("shared.STATE") as state:
            result = main.WindowApi().add_music_folder(folder)
        self.assertEqual(result, {"ok": True, "path": folder})
        state.add_local_root.assert_called_once_with(folder)

    def test_a_missing_folder_is_refused(self):
        with mock.patch("shared.STATE") as state:
            result = main.WindowApi().add_music_folder("/no/such/folder/rainette")
        self.assertFalse(result["ok"])
        state.add_local_root.assert_not_called()


class UpdateSwapWaitsForTheAppTests(unittest.TestCase):
    def test_swap_waits_for_the_native_app_as_well_as_the_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            bundle = Path(tmp) / "Rainette Music.app"
            (bundle / "Contents" / "MacOS").mkdir(parents=True)

            def fake_run(command, **kwargs):
                if command[0] == "/usr/bin/ditto":
                    (Path(command[3]) / "Rainette Music.app").mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(main, "_ENGINE_HOST_PID", 4242), \
                 mock.patch.object(main.sys, "executable", str(bundle / "Contents" / "MacOS" / "engine")), \
                 mock.patch.object(main.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(main.subprocess, "Popen") as popen:
                main._launch_macos_update(Path(tmp) / "update.zip", work)
                body = Path(popen.call_args.args[0][1]).read_text(encoding="utf-8")

        self.assertIn('host="4242"', body)
        self.assertIn('kill -0 "$host"', body)
        self.assertLess(body.index('kill -0 "$host"'), body.index('mv "$bundle"'))


if __name__ == "__main__":
    unittest.main()


class EngineExitCodeTests(unittest.TestCase):
    def test_only_an_update_quits_with_the_update_code(self):
        self.assertNotIn(main.ENGINE_EXIT_FOR_UPDATE, (0, 1, 128 + 15))
        swift = (ROOT / "macos" / "Sources" / "RainetteMusic" / "Model" / "AppModel.swift").read_text(encoding="utf-8")
        self.assertIn(f"engineExitForUpdate: Int32 = {main.ENGINE_EXIT_FOR_UPDATE}", swift)

    @unittest.skipUnless(sys.platform == "darwin", "the native app is macOS-only")
    def test_a_terminated_engine_does_not_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as home:
            engine = subprocess.Popen(
                [sys.executable, str(ROOT / "main.py"), "--engine"], cwd=ROOT, env={**os.environ, "HOME": home},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertTrue(engine.stdout.readline().startswith(main.ENGINE_HANDSHAKE_PREFIX))
                engine.terminate()
                self.assertEqual(engine.wait(timeout=15), 128 + 15)
            finally:
                if engine.poll() is None:
                    engine.kill()
