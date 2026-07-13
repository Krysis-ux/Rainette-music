import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

import server
import shared
from state import MusicState


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rainette-image"


class PlaylistArtworkHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = MusicState(self.root / "music.db")
        self.playlist = self.state.create_playlist("Artwork")
        self.old_state = shared.STATE
        self.old_policy = shared.POLICY
        self.old_artwork_dir = server.ARTWORK_DIR
        shared.STATE = self.state
        shared.POLICY = {"playlist_artwork_dir": self.root / "artwork"}
        server.ARTWORK_DIR = self.root / "artwork"
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()
        self.auth = {"X-Rainette-Token": server.APP_TOKEN}

    async def asyncTearDown(self):
        await self.client.close()
        server.ARTWORK_DIR = self.old_artwork_dir
        shared.STATE = self.old_state
        shared.POLICY = self.old_policy
        self.tmp.cleanup()

    def form(self, data=PNG_BYTES, *, content_type="image/png", filename="cover.png"):
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=content_type)
        return form

    async def test_upload_serve_replace_and_delete_artwork(self):
        response = await self.client.post(
            f"/playlist-artwork/{self.playlist['id']}", data=self.form(), headers=self.auth
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        first_key = payload["artwork_key"]
        self.assertEqual(self.state.get_playlist(self.playlist["id"])["artwork_key"], first_key)
        self.assertTrue((server.ARTWORK_DIR / first_key).is_file())

        served = await self.client.get(payload["artwork_url"])
        self.assertEqual(served.status, 200)
        self.assertEqual(await served.read(), PNG_BYTES)

        replaced = await self.client.post(
            f"/playlist-artwork/{self.playlist['id']}", data=self.form(PNG_BYTES + b"-new"), headers=self.auth
        )
        self.assertEqual(replaced.status, 200)
        second_key = (await replaced.json())["artwork_key"]
        self.assertNotEqual(second_key, first_key)
        self.assertFalse((server.ARTWORK_DIR / first_key).exists())

        deleted = await self.client.delete(f"/playlist-artwork/{self.playlist['id']}", headers=self.auth)
        self.assertEqual(deleted.status, 200)
        self.assertEqual(self.state.get_playlist(self.playlist["id"])["artwork_key"], "")
        self.assertFalse((server.ARTWORK_DIR / second_key).exists())

    async def test_upload_rejects_missing_playlist_corrupt_mismatch_and_oversize(self):
        missing = await self.client.post("/playlist-artwork/pl_missing", data=self.form(), headers=self.auth)
        corrupt = await self.client.post(
            f"/playlist-artwork/{self.playlist['id']}", data=self.form(b"not-a-png"), headers=self.auth
        )
        mismatch = await self.client.post(
            f"/playlist-artwork/{self.playlist['id']}",
            data=self.form(PNG_BYTES, content_type="image/jpeg", filename="wrong.jpg"),
            headers=self.auth,
        )
        oversize = await self.client.post(
            f"/playlist-artwork/{self.playlist['id']}",
            data=self.form(PNG_BYTES + b"x" * (server.MAX_PLAYLIST_ARTWORK_BYTES + 1)),
            headers=self.auth,
        )

        self.assertEqual(missing.status, 404)
        self.assertEqual(corrupt.status, 400)
        self.assertEqual(mismatch.status, 400)
        self.assertEqual(oversize.status, 413)

    async def test_artwork_get_rejects_path_traversal(self):
        response = await self.client.get("/playlist-artwork/..%2Fmusic.db")
        self.assertEqual(response.status, 404)

    async def test_artwork_mutations_require_launch_token(self):
        response = await self.client.post(
            f"/playlist-artwork/{self.playlist['id']}", data=self.form()
        )
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
