import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

import pwa_companion


class FakeRuntime:
    def __init__(self):
        self.commands = []

    async def command(self, payload):
        self.commands.append(payload)
        if payload["type"] == "music_stream_url":
            return {
                "id": payload["id"],
                "type": "music_stream_url_result",
                "ok": True,
                "url": "https://r1---sn.example.googlevideo.com/videoplayback?id=abc",
                "expires_hint_s": 3600,
            }
        return {"id": payload["id"], "type": payload["type"] + "_result", "ok": True}

    async def events(self, after, wait_s):
        return {"revision": after, "reset_required": False, "events": []}


class PwaCompanionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin = "https://rainette-music.vercel.app"
        self.token = "test-secret-token"
        self.runtime = FakeRuntime()
        self.relay = pwa_companion.RelayStore(now=lambda: 1_000)
        app = pwa_companion.build_app(
            runtime=self.runtime,
            access_token=self.token,
            allowed_origins={self.origin},
            relay_store=self.relay,
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_status_requires_bearer_token_and_allows_configured_origin(self):
        denied = await self.client.get("/status", headers={"Origin": self.origin})
        accepted = await self.client.get(
            "/status",
            headers={"Origin": self.origin, "Authorization": "Bearer " + self.token},
        )

        self.assertEqual(denied.status, 401)
        self.assertEqual(accepted.status, 200)
        self.assertEqual(accepted.headers.get("Access-Control-Allow-Origin"), self.origin)
        self.assertTrue((await accepted.json())["ok"])

    async def test_preflight_rejects_unknown_web_origins(self):
        allowed = await self.client.options(
            "/command",
            headers={
                "Origin": self.origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        denied = await self.client.options(
            "/command",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(allowed.status, 204)
        self.assertEqual(allowed.headers.get("Access-Control-Allow-Origin"), self.origin)
        self.assertIn("Authorization", allowed.headers.get("Access-Control-Allow-Headers", ""))
        self.assertEqual(denied.status, 403)

    async def test_only_mobile_music_commands_are_accepted(self):
        forbidden = await self.client.post(
            "/command",
            headers={"Origin": self.origin, "Authorization": "Bearer " + self.token},
            json={"type": "desktop_run_arbitrary_code", "id": "bad"},
        )
        accepted = await self.client.post(
            "/command",
            headers={"Origin": self.origin, "Authorization": "Bearer " + self.token},
            json={"type": "music_search", "id": "search-1", "query": "rain"},
        )

        self.assertEqual(forbidden.status, 400)
        self.assertEqual(accepted.status, 200)
        self.assertEqual(self.runtime.commands[-1]["type"], "music_search")

    async def test_stream_urls_are_replaced_with_short_lived_pc_relay_urls(self):
        response = await self.client.post(
            "/command",
            headers={"Origin": self.origin, "Authorization": "Bearer " + self.token},
            json={"type": "music_stream_url", "id": "stream-1", "source_id": "abc"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["url"].startswith("/audio/"))
        self.assertNotIn("googlevideo", payload["url"])
        grant = payload["url"].split("/audio/", 1)[1]
        self.assertIn("googlevideo.com", self.relay.resolve(grant))

    async def test_event_polling_is_authenticated(self):
        response = await self.client.get(
            "/events?after=7&wait=0",
            headers={"Origin": self.origin, "Authorization": "Bearer " + self.token},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["revision"], 7)


class RelayStoreTests(unittest.TestCase):
    def test_grants_expire(self):
        clock = [1_000]
        relay = pwa_companion.RelayStore(now=lambda: clock[0])
        grant = relay.create("https://example.googlevideo.com/audio", ttl_s=10)
        self.assertIsNotNone(relay.resolve(grant))
        clock[0] += 11
        self.assertIsNone(relay.resolve(grant))

    def test_only_expected_media_hosts_can_be_relayed(self):
        relay = pwa_companion.RelayStore(now=lambda: 1_000)
        with self.assertRaisesRegex(ValueError, "media host"):
            relay.create("https://evil.example/private", ttl_s=10)


if __name__ == "__main__":
    unittest.main()
