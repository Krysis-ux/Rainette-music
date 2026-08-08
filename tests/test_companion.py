import threading
import unittest

from aiohttp.test_utils import TestClient, TestServer

from companion import CompanionRegistry
import server


ORIGIN = "https://music-pwa-web.vercel.app"


def pair_device(registry, name="Phone"):
    """Run one full pairing handshake and return the device's credential."""
    invitation = registry.create_invitation(ttl_s=300)
    request = registry.request_pairing(invitation["token"], name)
    approved = registry.approve(request["request_id"])
    claimed = registry.pairing_result(request["request_id"], invitation["token"])
    return {**approved, "device_token": claimed["device_token"], "request_id": request["request_id"]}


class CompanionRegistryTests(unittest.TestCase):
    def test_invitation_requires_approval_before_device_is_authorized(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=60)

        self.assertFalse(registry.authorize(invitation["token"]))
        pending = registry.request_pairing(invitation["token"], "Lenno's iPhone")
        self.assertEqual(pending["status"], "pending")
        # Still nothing: asking is not the same as being allowed in.
        self.assertEqual(registry.devices(), [])

        approved = registry.approve(pending["request_id"])
        self.assertEqual(approved["device_name"], "Lenno's iPhone")
        claimed = registry.pairing_result(pending["request_id"], invitation["token"])
        self.assertTrue(registry.authorize(claimed["device_token"]))

    def test_each_paired_phone_receives_a_distinct_credential(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        first = pair_device(registry, "Phone A")
        second = pair_device(registry, "Phone B")

        self.assertNotEqual(first["device_id"], second["device_id"])
        self.assertNotEqual(first["device_token"], second["device_token"])

    def test_revoking_one_phone_leaves_every_other_phone_working(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        revoked = pair_device(registry, "Old phone")
        kept = pair_device(registry, "Current phone")

        registry.revoke(revoked["device_id"])

        self.assertFalse(registry.authorize(revoked["device_token"]))
        self.assertTrue(registry.authorize(kept["device_token"]))

    def test_pairing_result_requires_the_matching_invitation(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=60)
        request = registry.request_pairing(invitation["token"], "Phone")
        registry.approve(request["request_id"])

        self.assertIsNone(registry.pairing_result(request["request_id"], "not-the-invitation"))
        self.assertIn("device_token", registry.pairing_result(request["request_id"], invitation["token"]))

    def test_expired_invitation_cannot_create_a_pairing_request(self):
        clock = [1_000]
        registry = CompanionRegistry(now=lambda: clock[0])
        invitation = registry.create_invitation(ttl_s=1)
        clock[0] += 2

        with self.assertRaisesRegex(ValueError, "expired"):
            registry.request_pairing(invitation["token"], "Phone")

    def test_an_invitation_is_single_use(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=300)
        registry.request_pairing(invitation["token"], "First phone")

        with self.assertRaisesRegex(ValueError, "expired or invalid"):
            registry.request_pairing(invitation["token"], "Second phone")

    def test_device_names_are_length_capped(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=300)
        registry.request_pairing(invitation["token"], "N" * 500)

        self.assertLessEqual(len(registry.pending_requests()[0]["device_name"]), 60)

    def test_relay_grants_expire_and_die_with_their_device(self):
        clock = [1_000]
        registry = CompanionRegistry(now=lambda: clock[0])
        device = pair_device(registry)
        grant = registry.create_relay_grant(device["device_id"], "https://audio.example/track", ttl_s=30)

        self.assertEqual(registry.resolve_relay(grant["token"]), "https://audio.example/track")
        registry.revoke(device["device_id"])
        self.assertIsNone(registry.resolve_relay(grant["token"]))

        # And an unrevoked device's grant still stops working once it expires.
        other = pair_device(registry, "Other")
        expiring = registry.create_relay_grant(other["device_id"], "https://audio.example/b", ttl_s=30)
        clock[0] += 31
        self.assertIsNone(registry.resolve_relay(expiring["token"]))

    def test_unknown_relay_grants_are_never_resolved(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        pair_device(registry)

        self.assertIsNone(registry.resolve_relay("made-up-grant"))

    def test_pending_requests_can_be_rejected(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invite = registry.create_invitation(ttl_s=300)
        pending = registry.request_pairing(invite["token"], "Phone")

        listed = registry.pending_requests()[0]
        self.assertEqual(set(listed), {"request_id", "device_name", "status"})
        self.assertEqual(listed["request_id"], pending["request_id"])
        self.assertTrue(registry.reject(pending["request_id"]))
        self.assertEqual(registry.pending_requests(), [])
        self.assertFalse(registry.reject(pending["request_id"]))

    def test_expired_pending_requests_are_purged(self):
        clock = [1_000]
        registry = CompanionRegistry(now=lambda: clock[0])
        invite = registry.create_invitation(ttl_s=1)
        pending = registry.request_pairing(invite["token"], "Phone")

        clock[0] += 2

        self.assertEqual(registry.pending_requests(), [])
        self.assertFalse(registry.reject(pending["request_id"]))

    def test_devices_omit_credentials_and_show_revocation(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        device = pair_device(registry, "Phone")

        listed = registry.devices()[0]
        self.assertEqual(set(listed), {"device_id", "name", "revoked"})
        self.assertNotIn("device_token", listed)
        self.assertFalse(listed["revoked"])
        registry.revoke(device["device_id"])
        self.assertTrue(registry.devices()[0]["revoked"])

    def test_server_management_wrappers_delegate_to_registry(self):
        original = server.companion_registry
        registry = CompanionRegistry(now=lambda: 1_000)
        server.companion_registry = registry
        try:
            invite = registry.create_invitation(ttl_s=300)
            pending = registry.request_pairing(invite["token"], "Phone")

            self.assertEqual(server.companion_management_state(), {
                "pending": [{
                    "request_id": pending["request_id"],
                    "device_name": "Phone",
                    "status": "pending",
                }],
                "devices": [],
            })
            self.assertTrue(server.reject_companion_request(pending["request_id"]))
            self.assertFalse(server.reject_companion_request(pending["request_id"]))
        finally:
            server.companion_registry = original

    def test_server_approval_returns_only_sanitized_device_metadata(self):
        original = server.companion_registry
        registry = CompanionRegistry(now=lambda: 1_000)
        server.companion_registry = registry
        try:
            invite = registry.create_invitation(ttl_s=300)
            pending = registry.request_pairing(invite["token"], "Phone")

            approved = server.approve_companion_request(pending["request_id"])

            self.assertEqual(set(approved), {"device_id", "name", "revoked"})
            self.assertNotIn("device_token", approved)
        finally:
            server.companion_registry = original

    def test_registry_reads_wait_for_an_in_progress_mutation(self):
        mutation_entered = threading.Event()
        release_mutation = threading.Event()
        read_finished = threading.Event()

        def blocking_now():
            mutation_entered.set()
            release_mutation.wait(2)
            return 1_000

        registry = CompanionRegistry(now=blocking_now)
        mutation = threading.Thread(target=registry.create_invitation)
        read = threading.Thread(target=lambda: (registry.devices(), read_finished.set()))
        mutation.start()
        self.assertTrue(mutation_entered.wait(1))
        read.start()
        try:
            self.assertFalse(read_finished.wait(0.1))
        finally:
            release_mutation.set()
            mutation.join(2)
            read.join(2)
        self.assertTrue(read_finished.is_set())


class CompanionSessionIsolationTests(unittest.TestCase):
    """Two phones on one computer must not share playback state."""

    def setUp(self):
        self.broker = server.CompanionSyncBroker()
        # A log only exists once its phone has polled at least once.
        self.broker.read_after("phone-a", 0, 0)
        self.broker.read_after("phone-b", 0, 0)

    def events_for(self, device_id):
        return [item["message"] for item in self.broker.read_after(device_id, 0, 0)["events"]]

    def test_playback_events_reach_only_the_phone_that_caused_them(self):
        self.broker.publish({"type": "music_now_playing", "track": {"title": "A"}}, "phone-a")

        self.assertEqual(len(self.events_for("phone-a")), 1)
        self.assertEqual(self.events_for("phone-b"), [])

    def test_transport_control_from_one_phone_never_touches_another(self):
        for event_type in ("music_progress", "music_remote_control", "music_remote_play"):
            self.broker.publish({"type": event_type}, "phone-a")

        self.assertEqual(len(self.events_for("phone-a")), 3)
        self.assertEqual(self.events_for("phone-b"), [])

    def test_shared_library_changes_still_reach_every_phone(self):
        self.broker.publish({"type": "music_playlist_created", "id": "p1"}, "phone-a")

        # The catalog is one shared library on this computer, so both see it.
        self.assertEqual(len(self.events_for("phone-a")), 1)
        self.assertEqual(len(self.events_for("phone-b")), 1)

    def test_output_transfer_is_routed_to_its_target_not_its_origin(self):
        self.broker.publish(
            {"type": "music_output_transfer", "target_device_id": "phone-b"}, "phone-a"
        )

        self.assertEqual(self.events_for("phone-a"), [])
        self.assertEqual(len(self.events_for("phone-b")), 1)

    def test_desktop_playback_does_not_leak_into_any_phone_session(self):
        # An empty origin is the desktop's own windows.
        self.broker.publish({"type": "music_now_playing", "track": {"title": "A"}}, "")

        self.assertEqual(self.events_for("phone-a"), [])
        self.assertEqual(self.events_for("phone-b"), [])

    def test_revoked_device_loses_its_queued_events(self):
        self.broker.publish({"type": "music_now_playing"}, "phone-a")
        self.broker.forget("phone-a")

        self.assertEqual(self.events_for("phone-a"), [])


class RateLimiterTests(unittest.TestCase):
    def test_attempts_are_capped_per_caller_and_recover_after_the_window(self):
        clock = [1_000.0]
        limiter = server.RateLimiter(limit=3, window_s=60, now=lambda: clock[0])

        self.assertTrue(all(limiter.allow("1.2.3.4") for _ in range(3)))
        self.assertFalse(limiter.allow("1.2.3.4"))
        # A different caller has its own budget.
        self.assertTrue(limiter.allow("5.6.7.8"))

        clock[0] += 61
        self.assertTrue(limiter.allow("1.2.3.4"))


class CompanionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = CompanionRegistry(now=lambda: 1_000)
        app = server.build_companion_app(self.registry, allowed_origins={ORIGIN})
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_pairing_is_pending_then_protected_status_accepts_approved_device(self):
        invitation = self.registry.create_invitation(ttl_s=60)
        requested = await self.client.post(
            "/pair/request",
            json={"invitation": invitation["token"], "device_name": "iPhone"},
        )
        self.assertEqual(requested.status, 202)
        request_id = (await requested.json())["request_id"]

        pending = await self.client.post(
            "/pair/result", json={"request_id": request_id, "invitation": invitation["token"]}
        )
        self.assertEqual(pending.status, 202)

        self.registry.approve(request_id)
        claimed = await self.client.post(
            "/pair/result", json={"request_id": request_id, "invitation": invitation["token"]}
        )
        self.assertEqual(claimed.status, 200)
        token = (await claimed.json())["device_token"]

        denied = await self.client.get("/status")
        accepted = await self.client.get("/status", headers={"Authorization": "Bearer " + token})
        self.assertEqual(denied.status, 401)
        self.assertEqual(accepted.status, 200)

    async def test_unknown_web_origins_are_refused(self):
        allowed = await self.client.options(
            "/command",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
        )
        denied = await self.client.options(
            "/command",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )

        self.assertEqual(allowed.status, 204)
        self.assertEqual(allowed.headers.get("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(denied.status, 403)

    async def test_audio_relay_is_reachable_without_a_header_but_honours_revocation(self):
        """A browser cannot set headers on <audio>, so the grant is the credential."""
        device = pair_device(self.registry)
        grant = self.registry.create_relay_grant(device["device_id"], "https://a.googlevideo.com/x")

        self.registry.revoke(device["device_id"])
        revoked = await self.client.get("/audio/" + grant["token"])

        self.assertEqual(revoked.status, 404)

    async def test_polling_while_someone_walks_to_the_computer_is_not_throttled(self):
        """A phone polls every ~1.5s until a human approves it.

        The attempt limiter is far tighter than that on purpose, so the poll
        endpoint must have its own budget: otherwise a user who takes half a
        minute to reach their computer is rate-limited out of their own pairing.
        """
        invitation = self.registry.create_invitation(ttl_s=300)
        requested = await self.client.post(
            "/pair/request",
            headers={"Origin": ORIGIN},
            json={"invitation": invitation["token"], "device_name": "Slow walker"},
        )
        request_id = (await requested.json())["request_id"]

        body = {"request_id": request_id, "invitation": invitation["token"]}
        # Two minutes of polling at the client's real cadence.
        statuses = set()
        for _ in range(80):
            response = await self.client.post("/pair/result", headers={"Origin": ORIGIN}, json=body)
            statuses.add(response.status)

        self.assertEqual(statuses, {202})

    async def test_pairing_endpoints_are_rate_limited(self):
        limited = server.build_companion_app(
            CompanionRegistry(now=lambda: 1_000),
            allowed_origins={ORIGIN},
            pair_limiter=server.RateLimiter(limit=2, window_s=60),
        )
        client = TestClient(TestServer(limited))
        await client.start_server()
        try:
            statuses = [
                (await client.post("/pair/request", json={"invitation": "guess", "device_name": "x"})).status
                for _ in range(3)
            ]
        finally:
            await client.close()

        self.assertEqual(statuses[-1], 429)


if __name__ == "__main__":
    unittest.main()
