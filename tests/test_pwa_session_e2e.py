"""End-to-end proof that two phones paired to one computer stay independent.

These exercise the real HTTP surface a phone talks to — pair, approve, claim,
command, poll — rather than calling the registry directly, so a regression in
the routing or middleware layer is caught here too.
"""

import unittest

from aiohttp.test_utils import TestClient, TestServer

import music_bridge
import server
import shared
from companion import CompanionRegistry

ORIGIN = "https://music-pwa-web.vercel.app"


class TwoPhonesOnOneComputerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = CompanionRegistry(now=lambda: 1_000)
        self.broker = server.CompanionSyncBroker()
        app = server.build_companion_app(
            self.registry, sync_broker=self.broker, allowed_origins={ORIGIN}
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

        # Route bridge fan-out into this test's broker, the way server.start does.
        self._previous_notify = shared._notify
        shared.configure(state=shared.STATE, notify=self._fanout, policy=shared.POLICY)

    async def asyncTearDown(self):
        shared.configure(state=shared.STATE, notify=self._previous_notify, policy=shared.POLICY)
        await self.client.close()

    def _fanout(self, message):
        self.broker.publish(message, server._origin_device.get(""))

    async def pair_phone(self, name):
        """Complete the real pairing handshake over HTTP and return the token."""
        invitation = self.registry.create_invitation(ttl_s=300)
        requested = await self.client.post(
            "/pair/request",
            headers={"Origin": ORIGIN},
            json={"invitation": invitation["token"], "device_name": name},
        )
        self.assertEqual(requested.status, 202)
        request_id = (await requested.json())["request_id"]

        # Nothing is granted until the person at the computer approves.
        pending = await self.client.post(
            "/pair/result",
            headers={"Origin": ORIGIN},
            json={"request_id": request_id, "invitation": invitation["token"]},
        )
        self.assertEqual(pending.status, 202)

        self.registry.approve(request_id)
        claimed = await self.client.post(
            "/pair/result",
            headers={"Origin": ORIGIN},
            json={"request_id": request_id, "invitation": invitation["token"]},
        )
        self.assertEqual(claimed.status, 200)
        body = await claimed.json()

        auth = {"Origin": ORIGIN, "Authorization": "Bearer " + body["device_token"]}
        acked = await self.client.post("/pair/ack", headers=auth, json={"request_id": request_id})
        self.assertEqual(acked.status, 200)
        return {"headers": auth, "device_id": body["device_id"]}

    async def events_for(self, phone, after=0):
        response = await self.client.get(f"/events?after={after}&wait=0", headers=phone["headers"])
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_status_names_the_computer_the_phone_is_driving(self):
        # The phone shows this as "Playing from …", so it has to come back from
        # the gateway rather than being configured by hand.
        phone = await self.pair_phone("Studio iPhone")

        response = await self.client.get("/status", headers=phone["headers"])

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["name"])
        self.assertIn("pairing", payload["capabilities"])

    async def test_two_phones_pair_independently_and_do_not_share_playback(self):
        alice = await self.pair_phone("Alice's phone")
        bob = await self.pair_phone("Bob's phone")
        self.assertNotEqual(alice["device_id"], bob["device_id"])

        # Open both event streams so each phone has a live log.
        await self.events_for(alice)
        await self.events_for(bob)

        # Alice starts something playing.
        played = await self.client.post(
            "/command",
            headers=alice["headers"],
            json={
                "type": "music_now_playing_set",
                "id": "alice-1",
                "track": {"title": "Alice's track", "source_id": "a1"},
                "playing": True,
            },
        )
        self.assertEqual(played.status, 200)

        alice_events = await self.events_for(alice)
        bob_events = await self.events_for(bob)

        alice_types = [item["message"]["type"] for item in alice_events["events"]]
        self.assertIn("music_now_playing", alice_types)
        # Bob is listening to his own thing; Alice's playback must not reach him.
        self.assertEqual(bob_events["events"], [])

    async def test_revoking_one_phone_leaves_the_other_connected(self):
        keeper = await self.pair_phone("Keeper")
        doomed = await self.pair_phone("Doomed")

        self.registry.revoke(doomed["device_id"])
        self.broker.forget(doomed["device_id"])

        still_ok = await self.client.get("/status", headers=keeper["headers"])
        rejected = await self.client.get("/status", headers=doomed["headers"])

        self.assertEqual(still_ok.status, 200)
        self.assertEqual(rejected.status, 401)

    async def test_a_phone_cannot_redeem_another_phones_audio_grant_after_revocation(self):
        owner = await self.pair_phone("Owner")
        grant = self.registry.create_relay_grant(owner["device_id"], "https://a.googlevideo.com/x")

        # Valid while the device is live...
        self.assertIsNotNone(self.registry.resolve_relay(grant["token"]))
        # ...and dead the moment that device is revoked, so audio dies with access.
        self.registry.revoke(owner["device_id"])
        missing = await self.client.get("/audio/" + grant["token"], headers={"Origin": ORIGIN})
        self.assertEqual(missing.status, 404)

    async def test_shared_library_events_still_reach_both_phones(self):
        alice = await self.pair_phone("Alice")
        bob = await self.pair_phone("Bob")
        await self.events_for(alice)
        await self.events_for(bob)

        # A playlist is a property of the computer's one library, not of a phone.
        self.broker.publish({"type": "music_playlist_created", "id": "p1"}, alice["device_id"])

        self.assertEqual(len((await self.events_for(alice))["events"]), 1)
        self.assertEqual(len((await self.events_for(bob))["events"]), 1)

    async def test_command_allowlist_still_blocks_arbitrary_desktop_calls(self):
        phone = await self.pair_phone("Phone")

        blocked = await self.client.post(
            "/command",
            headers=phone["headers"],
            json={"type": "music_theme_set", "id": "x", "theme": "midnight"},
        )

        # Theme changes are a desktop concern and are not on the mobile allowlist.
        self.assertEqual(blocked.status, 400)
        self.assertIn("not allowed", (await blocked.json())["msg"])


if __name__ == "__main__":
    unittest.main()
