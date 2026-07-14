import asyncio
import base64

from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from companion import CompanionRegistry
import server


def async_test(function):
    def run():
        return asyncio.run(function())
    return run


def _phone_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private, public


def _approve(registry: CompanionRegistry):
    private, public = _phone_keypair()
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel", public)
    approved = registry.approve(request["request_id"])
    result = registry.pairing_result(request["request_id"], invitation["token"])
    token = private.decrypt(
        base64.b64decode(result["encrypted_device_token"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")
    return invitation, request, approved, token


@async_test
async def test_one_way_command_without_id_is_dispatched_and_accepted_immediately():
    registry = CompanionRegistry(now=lambda: 1_000)
    _, _, _, token = _approve(registry)
    seen = []
    original = server.music_bridge.DISPATCH["music_progress"]
    server.music_bridge.DISPATCH["music_progress"] = lambda message: seen.append(message.copy())
    client = TestClient(TestServer(server.build_companion_app(registry, command_timeout_s=10)))
    await client.start_server()
    try:
        response = await asyncio.wait_for(client.post(
            "/command",
            headers={"Authorization": "Bearer " + token},
            json={"type": "music_progress", "current_time": 12},
        ), timeout=0.25)
        payload = await response.json()
        assert response.status == 200
        assert payload == {
            "ok": True,
            "id": seen[0]["id"],
            "type": "music_progress_accepted",
        }
        assert seen[0]["current_time"] == 12
        assert server.command_broker.pending_count == 0
    finally:
        await client.close()
        server.music_bridge.DISPATCH["music_progress"] = original


@async_test
async def test_one_way_command_with_explicit_id_does_not_wait_for_uncorrelated_bridge_event():
    registry = CompanionRegistry(now=lambda: 1_000)
    _, _, _, token = _approve(registry)
    original = server.music_bridge.DISPATCH["music_now_playing_set"]
    server.music_bridge.DISPATCH["music_now_playing_set"] = lambda _message: None
    client = TestClient(TestServer(server.build_companion_app(registry, command_timeout_s=0.01)))
    await client.start_server()
    try:
        response = await client.post(
            "/command",
            headers={"Authorization": "Bearer " + token},
            json={"type": "music_now_playing_set", "id": "phone-state", "track": {}},
        )
        assert response.status == 200
        assert await response.json() == {
            "ok": True,
            "id": "phone-state",
            "type": "music_now_playing_set_accepted",
        }
        assert server.command_broker.pending_count == 0
    finally:
        await client.close()
        server.music_bridge.DISPATCH["music_now_playing_set"] = original


@async_test
async def test_approved_pair_result_repeats_until_authenticated_ack():
    registry = CompanionRegistry(now=lambda: 1_000)
    invitation, request, approved, token = _approve(registry)
    client = TestClient(TestServer(server.build_companion_app(registry)))
    await client.start_server()
    try:
        proof = {"request_id": request["request_id"], "invitation": invitation["token"]}
        first = await client.post("/pair/result", json=proof)
        second = await client.post("/pair/result", json=proof)
        assert first.status == second.status == 200
        assert await first.json() == await second.json()

        ack = await client.post(
            "/pair/ack",
            headers={"Authorization": "Bearer " + token},
            json={"request_id": request["request_id"]},
        )
        assert ack.status == 200
        assert await ack.json() == {"ok": True, "request_id": request["request_id"]}

        gone = await client.post("/pair/result", json=proof)
        second_ack = await client.post(
            "/pair/ack",
            headers={"Authorization": "Bearer " + token},
            json={"request_id": request["request_id"]},
        )
        assert gone.status == 404
        assert second_ack.status == 404
        assert registry.authorize(token)
        assert registry.devices()[0]["device_id"] == approved["device_id"]
    finally:
        await client.close()


def test_unclaimed_claim_cleanup_revokes_device_without_result_poll(tmp_path):
    clock = [1_000]
    registry = CompanionRegistry(now=lambda: clock[0], storage_path=tmp_path / "devices.json", claim_ttl_s=10)
    _, _, _, token = _approve(registry)
    clock[0] = 1_011

    assert registry.devices() == []
    assert not registry.authorize(token)


def test_unclaimed_claim_cleanup_survives_registry_restart(tmp_path):
    clock = [1_000]
    storage = tmp_path / "devices.json"
    registry = CompanionRegistry(now=lambda: clock[0], storage_path=storage, claim_ttl_s=10)
    _, _, _, token = _approve(registry)
    clock[0] = 1_011

    restarted = CompanionRegistry(now=lambda: clock[0], storage_path=storage, claim_ttl_s=10)
    assert restarted.devices() == []
    assert not restarted.authorize(token)


def test_security_reads_observe_revocation_from_a_second_registry(tmp_path):
    storage = tmp_path / "devices.json"
    first = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    _, _, approved, token = _approve(first)
    second = CompanionRegistry(now=lambda: 1_000, storage_path=storage)

    assert second.revoke(approved["device_id"])
    assert not first.authorize(token)
    assert first.device_id_for_token(token) is None
    assert first.devices() == [{
        "device_id": approved["device_id"], "name": "Pixel", "revoked": True,
    }]


@async_test
async def test_status_advertises_pairing_library_events_and_output_transfer_capabilities():
    registry = CompanionRegistry(now=lambda: 1_000)
    _, _, _, token = _approve(registry)
    client = TestClient(TestServer(server.build_companion_app(registry)))
    await client.start_server()
    try:
        response = await client.get("/status", headers={"Authorization": "Bearer " + token})
        assert response.status == 200
        assert (await response.json())["capabilities"] == ["pairing", "library", "events", "output-transfer"]
    finally:
        await client.close()
