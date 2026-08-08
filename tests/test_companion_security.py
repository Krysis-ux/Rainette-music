import json
import unittest
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from companion import CompanionRegistry
import server


ORIGIN = "https://music-pwa-web.vercel.app"


def test_first_valid_pairing_request_consumes_invitation():
    registry = CompanionRegistry(now=lambda: 1_000)
    invitation = registry.create_invitation(ttl_s=60)
    first = registry.request_pairing(invitation["token"], "Pixel")

    with pytest.raises(ValueError, match="expired or invalid"):
        registry.request_pairing(invitation["token"], "Other phone")

    assert first["status"] == "pending"


def test_rejection_is_visible_to_original_phone_only():
    registry = CompanionRegistry(now=lambda: 1_000)
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel")

    assert registry.reject(request["request_id"])
    assert registry.pairing_result(request["request_id"], invitation["token"]) == {"status": "rejected"}
    assert registry.pairing_result(request["request_id"], "wrong") is None


def test_registry_persists_only_hashed_device_credentials_and_revocation(tmp_path):
    storage = tmp_path / "companion-devices.json"
    first = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    invitation = first.create_invitation(ttl_s=60)
    request = first.request_pairing(invitation["token"], "Pixel")
    approved = first.approve(request["request_id"])
    result = first.pairing_result(request["request_id"], invitation["token"])
    token = result["device_token"]

    persisted = storage.read_text(encoding="utf-8")
    assert token not in persisted
    assert invitation["token"] not in persisted
    assert result["device_token"] not in persisted
    assert set(json.loads(persisted)["devices"][0]) == {"device_id", "name", "token_hash", "revoked"}

    restarted = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    assert restarted.authorize(token)
    assert restarted.revoke(approved["device_id"])
    restarted_again = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    assert not restarted_again.authorize(token)
    assert restarted_again.devices()[0]["revoked"] is True


def test_approved_claim_ttl_is_independent_and_expiry_removes_unclaimed_device(tmp_path):
    clock = [1_000.0]
    storage = tmp_path / "companion-devices.json"
    registry = CompanionRegistry(now=lambda: clock[0], storage_path=storage, claim_ttl_s=120)
    invitation = registry.create_invitation(ttl_s=10)
    request = registry.request_pairing(invitation["token"], "Pixel")
    clock[0] = 1_009
    approved = registry.approve(request["request_id"])

    # The original invitation has expired, but the separate post-approval claim has not.
    clock[0] = 1_011
    claim = registry.pairing_result(request["request_id"], invitation["token"])
    assert claim["status"] == "approved"
    assert claim["device_token"]
    assert registry.acknowledge_pairing(request["request_id"], approved["device_id"])

    invitation2 = registry.create_invitation(ttl_s=10)
    request2 = registry.request_pairing(invitation2["token"], "Unclaimed")
    clock[0] = 1_020
    approved2 = registry.approve(request2["request_id"])
    clock[0] = 1_141

    assert registry.pairing_result(request2["request_id"], invitation2["token"]) == {"status": "expired"}
    assert approved2["device_id"] not in {device["device_id"] for device in registry.devices()}
    restarted = CompanionRegistry(now=lambda: clock[0], storage_path=storage)
    assert approved2["device_id"] not in {device["device_id"] for device in restarted.devices()}
    assert approved["device_id"] in {device["device_id"] for device in restarted.devices()}


def test_authenticated_pairing_ack_survives_desktop_restart_and_cancels_claim_expiry(tmp_path):
    clock = [1_000.0]
    storage = tmp_path / "companion-devices.json"
    registry = CompanionRegistry(now=lambda: clock[0], storage_path=storage, claim_ttl_s=10)
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel")
    approved = registry.approve(request["request_id"])
    result = registry.pairing_result(request["request_id"], invitation["token"])
    token = result["device_token"]

    restarted = CompanionRegistry(now=lambda: clock[0], storage_path=storage, claim_ttl_s=10)
    authenticated_device = restarted.device_id_for_token(token)
    assert authenticated_device == approved["device_id"]
    assert restarted.acknowledge_pairing(request["request_id"], authenticated_device)

    persisted = json.loads(storage.read_text(encoding="utf-8"))
    assert persisted["claims"] == []
    assert token not in storage.read_text(encoding="utf-8")

    clock[0] = 1_011
    after_ttl = CompanionRegistry(now=lambda: clock[0], storage_path=storage, claim_ttl_s=10)
    assert after_ttl.authorize(token)
    assert after_ttl.acknowledge_pairing(request["request_id"], approved["device_id"]) is False


def test_restarted_pairing_ack_rejects_wrong_request_and_authenticated_device(tmp_path):
    storage = tmp_path / "companion-devices.json"
    registry = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel")
    approved = registry.approve(request["request_id"])
    result = registry.pairing_result(request["request_id"], invitation["token"])
    token = result["device_token"]

    restarted = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    assert not restarted.acknowledge_pairing("wrong-request", approved["device_id"])
    assert not restarted.acknowledge_pairing(request["request_id"], "wrong-device")
    assert restarted.authorize(token)
    assert len(json.loads(storage.read_text(encoding="utf-8"))["claims"]) == 1


def test_approval_persist_failure_leaves_request_pending_and_device_unauthorized(tmp_path, monkeypatch):
    registry = CompanionRegistry(now=lambda: 1_000, storage_path=tmp_path / "devices.json")
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel")

    def fail(_devices=None):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_persist_devices", fail)
    with pytest.raises(OSError, match="disk full"):
        registry.approve(request["request_id"])

    assert registry.devices() == []
    assert registry.pending_requests()[0]["request_id"] == request["request_id"]


def test_revoke_persist_failure_keeps_existing_device_authorized(tmp_path, monkeypatch):
    storage = tmp_path / "devices.json"
    registry = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel")
    approved = registry.approve(request["request_id"])
    claim = registry.pairing_result(request["request_id"], invitation["token"])
    token = claim["device_token"]

    def fail(_devices=None):
        raise OSError("read only")

    monkeypatch.setattr(registry, "_persist_devices", fail)
    with pytest.raises(OSError, match="read only"):
        registry.revoke(approved["device_id"])

    assert registry.authorize(token)
    assert registry.devices()[0]["revoked"] is False


def test_two_registry_instances_merge_mutations_without_lost_updates(tmp_path):
    storage = tmp_path / "devices.json"
    first = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    second = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    first_invite = first.create_invitation(ttl_s=60)
    second_invite = second.create_invitation(ttl_s=60)
    first_request = first.request_pairing(first_invite["token"], "Pixel")
    second_request = second.request_pairing(second_invite["token"], "iPhone")

    first_device = first.approve(first_request["request_id"])
    second_device = second.approve(second_request["request_id"])

    restarted = CompanionRegistry(now=lambda: 1_000, storage_path=storage)
    assert {item["device_id"] for item in restarted.devices()} == {
        first_device["device_id"], second_device["device_id"]
    }


class CompanionPairingHttpSecurityTests(unittest.IsolatedAsyncioTestCase):
  async def test_approved_result_repeats_until_ack_and_authorizes_status(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    client = TestClient(TestServer(server.build_companion_app(registry)))
    await client.start_server()
    try:
        invitation = registry.create_invitation(ttl_s=60)
        requested = await client.post("/pair/request", json={
            "invitation": invitation["token"], "device_name": "Pixel",
        })
        request_id = (await requested.json())["request_id"]

        pending = await client.post("/pair/result", json={
            "request_id": request_id, "invitation": invitation["token"],
        })
        assert pending.status == 202
        assert (await pending.json())["status"] == "pending"

        server_approval = registry.approve(request_id)
        assert "device_token" not in server_approval
        claimed = await client.post("/pair/result", json={
            "request_id": request_id, "invitation": invitation["token"],
        })
        payload = await claimed.json()
        token = payload["device_token"]
        status = await client.get("/status", headers={"Authorization": "Bearer " + token})
        assert status.status == 200
        assert (await status.json())["device_id"] == server_approval["device_id"]

        second = await client.post("/pair/result", json={
            "request_id": request_id, "invitation": invitation["token"],
        })
        assert second.status == 200
        assert (await second.json())["device_token"] == payload["device_token"]
        acknowledged = await client.post(
            "/pair/ack",
            headers={"Authorization": "Bearer " + token},
            json={"request_id": request_id},
        )
        assert acknowledged.status == 200
        gone = await client.post("/pair/result", json={
            "request_id": request_id, "invitation": invitation["token"],
        })
        assert gone.status == 404
    finally:
        await client.close()


  async def test_pair_result_cannot_be_claimed_with_wrong_request_or_invitation(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    client = TestClient(TestServer(server.build_companion_app(registry)))
    await client.start_server()
    try:
        invitation = registry.create_invitation(ttl_s=60)
        response = await client.post("/pair/request", json={
            "invitation": invitation["token"], "device_name": "Pixel",
        })
        request_id = (await response.json())["request_id"]
        registry.approve(request_id)
        for bad in (
            {"request_id": "wrong", "invitation": invitation["token"]},
            {"request_id": request_id, "invitation": "wrong"},
        ):
            denied = await client.post("/pair/result", json=bad)
            assert denied.status == 404
        valid = await client.post("/pair/result", json={
            "request_id": request_id, "invitation": invitation["token"],
        })
        assert valid.status == 200
    finally:
        await client.close()


def test_server_registry_uses_app_data_persistence():
    assert server.companion_registry.storage_path == server.APP_DATA_DIR / "companion-devices.json"


def test_destructive_local_data_commands_are_not_reachable_from_the_lan():
    """Erasing the user's library is a desktop-only action.

    The desktop WebSocket is token-gated and dispatches anything registered in
    music_bridge.DISPATCH, so a new command is reachable from the LAN the moment
    it is added to COMPANION_COMMAND_TYPES. These must never be: a paired phone
    should not be able to wipe recents, playlists, or follows over the network.
    """
    import music_bridge

    for command in ("music_recent_delete", "music_clear_data"):
        assert command in music_bridge.DISPATCH, f"{command} should be dispatchable on the desktop"
        assert command not in server.COMPANION_COMMAND_TYPES, (
            f"{command} is destructive and must not be invocable by a paired phone"
        )
        assert command not in server.COMPANION_ONE_WAY_COMMAND_TYPES
