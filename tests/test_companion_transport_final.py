import asyncio
import base64
import threading
import unittest

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from companion import CompanionRegistry
import server


def _phone_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private, public


def _approve(registry: CompanionRegistry) -> str:
    private, public = _phone_keypair()
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel", public)
    registry.approve(request["request_id"])
    result = registry.pairing_result(request["request_id"], invitation["token"])
    return private.decrypt(
        base64.b64decode(result["encrypted_device_token"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")


class CompanionCommandHttpTests(unittest.IsolatedAsyncioTestCase):
 async def test_authenticated_command_awaits_threaded_dispatch_result_with_matching_id(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    token = _approve(registry)
    request_id = "transport-threaded-result"
    original = server.music_bridge.DISPATCH.get("music_library_index")

    def deterministic_handler(message):
        def finish():
            server.hub.broadcast({
                "type": "music_library_index_result",
                "id": message["id"],
                "ok": True,
                "tracks": [{"title": "From desktop"}],
            })
        threading.Thread(target=finish, daemon=True).start()

    server.music_bridge.DISPATCH["music_library_index"] = deterministic_handler
    client = TestClient(TestServer(server.build_companion_app(registry, command_timeout_s=1)))
    await client.start_server()
    try:
        response = await client.post(
            "/command",
            headers={"Authorization": "Bearer " + token},
            json={"type": "music_library_index", "id": request_id, "limit": 12},
        )
        assert response.status == 200
        assert await response.json() == {
            "type": "music_library_index_result",
            "id": request_id,
            "ok": True,
            "tracks": [{"title": "From desktop"}],
        }
    finally:
        await client.close()
        if original is None:
            server.music_bridge.DISPATCH.pop("music_library_index", None)
        else:
            server.music_bridge.DISPATCH["music_library_index"] = original


 async def test_command_rejects_unauthorized_unknown_and_non_music_messages(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    token = _approve(registry)
    client = TestClient(TestServer(server.build_companion_app(registry, command_timeout_s=0.1)))
    await client.start_server()
    try:
        unauthorized = await client.post(
            "/command", json={"type": "music_library_index", "id": "unauthorized"}
        )
        unknown = await client.post(
            "/command",
            headers={"Authorization": "Bearer " + token},
            json={"type": "music_delete_everything", "id": "unknown"},
        )
        non_music = await client.post(
            "/command",
            headers={"Authorization": "Bearer " + token},
            json={"type": "shutdown", "id": "non-music"},
        )
        assert unauthorized.status == 401
        assert unknown.status == 400
        assert non_music.status == 400
    finally:
        await client.close()


 async def test_command_timeout_removes_response_listener(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    token = _approve(registry)
    original = server.music_bridge.DISPATCH.get("music_library_index")
    server.music_bridge.DISPATCH["music_library_index"] = lambda _message: None
    client = TestClient(TestServer(server.build_companion_app(registry, command_timeout_s=0.01)))
    await client.start_server()
    try:
        response = await client.post(
            "/command",
            headers={"Authorization": "Bearer " + token},
            json={"type": "music_library_index", "id": "will-time-out"},
        )
        assert response.status == 504
        await asyncio.sleep(0)
        assert server.command_broker.pending_count == 0
    finally:
        await client.close()
        if original is None:
            server.music_bridge.DISPATCH.pop("music_library_index", None)
        else:
            server.music_bridge.DISPATCH["music_library_index"] = original
