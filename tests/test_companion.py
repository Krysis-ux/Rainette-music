import base64
import tempfile
import threading
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from companion import CompanionRegistry, ensure_tls_certificate
import server


PHONE_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PHONE_PUBLIC_KEY = PHONE_PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode("ascii")


def claim_token(registry, request_id, invitation_token):
    result = registry.pairing_result(request_id, invitation_token)
    return PHONE_PRIVATE_KEY.decrypt(
        base64.b64decode(result["encrypted_device_token"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")


class CompanionRegistryTests(unittest.TestCase):
    def _approve_test_device(self, registry, name="Pixel"):
        invitation = registry.create_invitation(ttl_s=300)
        request = registry.request_pairing(invitation["token"], name, PHONE_PUBLIC_KEY)
        approved = registry.approve(request["request_id"])
        approved["device_token"] = claim_token(registry, request["request_id"], invitation["token"])
        return approved

    def test_tls_certificate_is_persisted_and_has_a_stable_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ensure_tls_certificate(root)
            second = ensure_tls_certificate(root)
            self.assertTrue(first.cert_path.is_file())
            self.assertTrue(first.key_path.is_file())
            self.assertEqual(first.fingerprint_sha256, second.fingerprint_sha256)
            self.assertIsNotNone(first.ssl_context)

    def test_invitation_requires_approval_before_device_is_authorized(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=60)

        self.assertFalse(registry.authorize(invitation["token"]))
        pending = registry.request_pairing(invitation["token"], "Lenno's Pixel", PHONE_PUBLIC_KEY)
        self.assertEqual(pending["status"], "pending")

        approved = registry.approve(pending["request_id"])
        self.assertEqual(approved["device_name"], "Lenno's Pixel")
        token = claim_token(registry, pending["request_id"], invitation["token"])
        self.assertTrue(registry.authorize(token))

    def test_expired_invitation_cannot_create_a_pairing_request(self):
        clock = [1_000]
        registry = CompanionRegistry(now=lambda: clock[0])
        invitation = registry.create_invitation(ttl_s=1)
        clock[0] += 2

        with self.assertRaisesRegex(ValueError, "expired"):
            registry.request_pairing(invitation["token"], "Phone", PHONE_PUBLIC_KEY)

    def test_revoked_device_loses_access_and_relay_grants_are_device_scoped(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=60)
        request = registry.request_pairing(invitation["token"], "Phone", PHONE_PUBLIC_KEY)
        device = registry.approve(request["request_id"])
        device["device_token"] = claim_token(registry, request["request_id"], invitation["token"])
        relay = registry.create_relay_grant(device["device_id"], "https://audio.example/track", ttl_s=30)

        self.assertEqual(registry.resolve_relay(relay["token"], device["device_token"]), "https://audio.example/track")
        registry.revoke(device["device_id"])
        self.assertFalse(registry.authorize(device["device_token"]))
        self.assertIsNone(registry.resolve_relay(relay["token"], device["device_token"]))

    def test_pending_requests_can_be_rejected(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        invite = registry.create_invitation(ttl_s=300)
        pending = registry.request_pairing(invite["token"], "Pixel", PHONE_PUBLIC_KEY)

        listed = registry.pending_requests()[0]
        self.assertEqual(set(listed), {"request_id", "device_name", "status"})
        self.assertEqual(listed["request_id"], pending["request_id"])
        self.assertEqual(listed["device_name"], "Pixel")
        self.assertEqual(listed["status"], "pending")
        self.assertTrue(registry.reject(pending["request_id"]))
        self.assertEqual(registry.pending_requests(), [])
        self.assertFalse(registry.reject(pending["request_id"]))

    def test_expired_pending_requests_are_purged(self):
        clock = [1_000]
        registry = CompanionRegistry(now=lambda: clock[0])
        invite = registry.create_invitation(ttl_s=1)
        pending = registry.request_pairing(invite["token"], "Pixel", PHONE_PUBLIC_KEY)

        clock[0] += 2

        self.assertEqual(registry.pending_requests(), [])
        self.assertFalse(registry.reject(pending["request_id"]))

    def test_devices_omit_credentials_and_show_revocation(self):
        registry = CompanionRegistry(now=lambda: 1_000)
        device = self._approve_test_device(registry)

        listed = registry.devices()[0]
        self.assertEqual(set(listed), {"device_id", "name", "revoked"})
        self.assertNotIn("device_token", listed)
        self.assertEqual(listed["device_id"], device["device_id"])
        self.assertEqual(listed["name"], "Pixel")
        self.assertFalse(listed["revoked"])
        registry.revoke(device["device_id"])
        self.assertTrue(registry.devices()[0]["revoked"])

    def test_server_management_wrappers_delegate_to_registry(self):
        original = server.companion_registry
        registry = CompanionRegistry(now=lambda: 1_000)
        server.companion_registry = registry
        try:
            invite = registry.create_invitation(ttl_s=300)
            pending = registry.request_pairing(invite["token"], "Pixel", PHONE_PUBLIC_KEY)

            self.assertEqual(server.companion_management_state(), {
                "pending": [{
                    "request_id": pending["request_id"],
                    "device_name": "Pixel",
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
            pending = registry.request_pairing(invite["token"], "Pixel", PHONE_PUBLIC_KEY)

            approved = server.approve_companion_request(pending["request_id"])

            self.assertEqual(set(approved), {"device_id", "name", "revoked"})
            self.assertEqual(approved["name"], "Pixel")
            self.assertFalse(approved["revoked"])
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
        self.assertFalse(mutation.is_alive())
        self.assertFalse(read.is_alive())
        self.assertTrue(read_finished.is_set())


class CompanionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = CompanionRegistry(now=lambda: 1_000)
        self.client = TestClient(TestServer(server.build_companion_app(self.registry)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_pairing_is_pending_then_protected_status_accepts_approved_device(self):
        invitation = self.registry.create_invitation(ttl_s=60)
        requested = await self.client.post(
            "/pair/request",
            json={"invitation": invitation["token"], "device_name": "Pixel", "public_key": PHONE_PUBLIC_KEY},
        )
        self.assertEqual(requested.status, 202)
        request_id = (await requested.json())["request_id"]
        approved = self.registry.approve(request_id)
        approved["device_token"] = claim_token(self.registry, request_id, invitation["token"])

        denied = await self.client.get("/status")
        accepted = await self.client.get("/status", headers={"Authorization": "Bearer " + approved["device_token"]})
        self.assertEqual(denied.status, 401)
        self.assertEqual(accepted.status, 200)
        self.assertEqual((await accepted.json())["device_id"], approved["device_id"])

    async def test_relay_grant_cannot_be_used_by_another_approved_device(self):
        def approve(name):
            invitation = self.registry.create_invitation(ttl_s=60)
            request = self.registry.request_pairing(invitation["token"], name, PHONE_PUBLIC_KEY)
            approved = self.registry.approve(request["request_id"])
            approved["device_token"] = claim_token(self.registry, request["request_id"], invitation["token"])
            return approved

        owner = approve("Owner")
        other = approve("Other")
        grant = self.registry.create_relay_grant(owner["device_id"], "https://audio.example/track")
        response = await self.client.get(
            "/audio/" + grant["token"],
            headers={"Authorization": "Bearer " + other["device_token"]},
        )
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["msg"], "relay grant is not available")


if __name__ == "__main__":
    unittest.main()
