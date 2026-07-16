import asyncio
import base64
import json
import socket
import ssl
import threading
import unittest
import urllib.request

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
        padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")


def _approve_and_ack(registry: CompanionRegistry) -> tuple[str, str]:
    private, public = _phone_keypair()
    invitation = registry.create_invitation(ttl_s=60)
    request = registry.request_pairing(invitation["token"], "Pixel", public)
    approved = registry.approve(request["request_id"])
    result = registry.pairing_result(request["request_id"], invitation["token"])
    token = private.decrypt(
        base64.b64decode(result["encrypted_device_token"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")
    assert registry.acknowledge_pairing(request["request_id"], approved["device_id"])
    return token, approved["device_id"]


def _companion_status(port: int, token: str) -> dict:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        f"https://127.0.0.1:{port}/status",
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, timeout=5, context=context) as response:
        return json.load(response)


def test_paired_listener_restarts_on_persisted_endpoint_and_reauthenticates(tmp_path, monkeypatch):
    server.stop_companion()
    storage = tmp_path / "companion-devices.json"
    registry = CompanionRegistry(storage_path=storage)
    token, device_id = _approve_and_ack(registry)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        configured_port = int(reservation.getsockname()[1])

    monkeypatch.setattr(server, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "companion_registry", registry)
    monkeypatch.setenv("RAINETTE_COMPANION_PORT", str(configured_port))
    try:
        first = server.start_paired_companion()
        assert first is not None
        assert first["port"] == configured_port
        assert (tmp_path / server.COMPANION_PORT_FILENAME).read_text(encoding="ascii") == str(configured_port)
        assert _companion_status(configured_port, token)["device_id"] == device_id
        first_fingerprint = first["certificate"].fingerprint_sha256
        assert server.stop_companion()

        # Simulate a new desktop process: security state and certificate are
        # loaded from disk, while a conflicting environment preference must
        # not move an endpoint already pinned by the phone.
        restarted_registry = CompanionRegistry(storage_path=storage)
        monkeypatch.setattr(server, "companion_registry", restarted_registry)
        other_port = 47879 if configured_port != 47879 else 47880
        monkeypatch.setenv("RAINETTE_COMPANION_PORT", str(other_port))
        second = server.start_paired_companion()
        assert second is not None
        assert second["port"] == configured_port
        assert second["certificate"].fingerprint_sha256 == first_fingerprint
        assert _companion_status(configured_port, token)["device_id"] == device_id
    finally:
        server.stop_companion()


def test_busy_persisted_port_is_not_silently_changed_for_paired_devices(tmp_path, monkeypatch):
    server.stop_companion()
    storage = tmp_path / "companion-devices.json"
    registry = CompanionRegistry(storage_path=storage)
    _approve_and_ack(registry)
    monkeypatch.setattr(server, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "companion_registry", registry)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        pinned_port = int(blocker.getsockname()[1])
        server._persist_companion_port(pinned_port)
        with pytest.raises(RuntimeError, match="no companion LAN port is available"):
            server.start_paired_companion()

    assert server._read_companion_port() == pinned_port
    assert not server._companion_runtime


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


 async def test_events_requires_authentication_and_replays_revisioned_messages(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    token = _approve(registry)
    sync_broker = server.CompanionSyncBroker(history_limit=4)
    sync_broker.publish({"type": "music_now_playing", "track": {"title": "From desktop"}})
    client = TestClient(TestServer(server.build_companion_app(registry, sync_broker=sync_broker)))
    await client.start_server()
    try:
        denied = await client.get("/events?after=0&wait=0")
        assert denied.status == 401

        response = await client.get(
            "/events?after=0&wait=0",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status == 200
        body = await response.json()
        assert body["ok"] is True
        assert body["revision"] == 1
        assert body["reset_required"] is False
        assert body["events"] == [{
            "revision": 1,
            "message": {"type": "music_now_playing", "track": {"title": "From desktop"}},
        }]
    finally:
        await client.close()


 async def test_events_require_reset_when_client_revision_is_ahead_after_desktop_restart(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    token = _approve(registry)
    sync_broker = server.CompanionSyncBroker()
    client = TestClient(TestServer(server.build_companion_app(registry, sync_broker=sync_broker)))
    await client.start_server()
    try:
        reset = await client.get(
            "/events?after=57&wait=0",
            headers={"Authorization": "Bearer " + token},
        )
        assert reset.status == 200
        assert await reset.json() == {
            "ok": True,
            "device_id": registry.device_id_for_token(token),
            "revision": 0,
            "reset_required": True,
            "events": [],
        }

        sync_broker.publish({"type": "music_now_playing", "track": {"title": "After restart"}})
        replay = await client.get(
            "/events?after=0&wait=0",
            headers={"Authorization": "Bearer " + token},
        )
        replay_payload = await replay.json()
        assert replay_payload["reset_required"] is False
        assert replay_payload["revision"] == 1
        assert replay_payload["events"][0]["message"]["track"]["title"] == "After restart"
    finally:
        await client.close()


 async def test_sync_broker_replays_actual_library_playlist_follow_and_queue_event_names(self):
    broker = server.CompanionSyncBroker()
    event_types = [
        "music_library_index_result",
        "music_recent_result",
        "music_top_artists_result",
        "music_insights_result",
        "music_artist_followed",
        "music_artist_unfollowed",
        "music_playlist_created",
        "music_playlist_renamed",
        "music_playlist_deleted",
        "music_playlist_meta_updated",
        "music_playlist_folder_created",
        "music_playlist_folder_renamed",
        "music_playlist_folder_deleted",
        "music_playlist_folder_moved",
        "music_smart_playlist_created",
        "music_smart_playlist_updated",
        "music_smart_playlist_deleted",
        "music_smart_playlist_tracks_result",
        "music_playlist_track_added",
        "music_playlist_track_removed",
        "music_queue_session_saved",
        "music_queue_session_deleted",
    ]
    for event_type in event_types:
        broker.publish({"type": event_type, "ok": True})

    replay = broker.read_after(0, 0)
    assert replay["revision"] == len(event_types)
    assert [event["message"]["type"] for event in replay["events"]] == event_types


 async def test_output_transfer_waits_for_target_confirmation_before_acknowledging(self):
    registry = CompanionRegistry(now=lambda: 1_000)
    token = _approve(registry)
    original_notify = server.shared._notify
    server.shared._notify = server.hub.broadcast

    client = TestClient(TestServer(server.build_companion_app(registry, command_timeout_s=1)))
    await client.start_server()
    try:
        transfer = asyncio.create_task(client.post(
            "/command", headers={"Authorization": "Bearer " + token},
            json={
                "type": "music_output_transfer", "id": "handoff",
                "source_device_id": "phone", "target_device_id": "desktop",
                "queue": [{"title": "Transfer me"}],
            },
        ))
        for _ in range(100):
            if server.command_broker.pending_count:
                break
            await asyncio.sleep(0.005)
        assert server.command_broker.pending_count == 1

        acknowledged = await client.post(
            "/command", headers={"Authorization": "Bearer " + token},
            json={
                "type": "music_output_transfer_result", "id": "handoff",
                "ok": True, "source_device_id": "phone", "target_device_id": "desktop",
            },
        )
        assert acknowledged.status == 200
        assert (await acknowledged.json())["type"] == "music_output_transfer_result_accepted"

        response = await transfer
        assert response.status == 200
        assert await response.json() == {
            "type": "music_output_transfer_result", "id": "handoff",
            "ok": True, "target_device_id": "desktop", "source_device_id": "phone",
        }
    finally:
        await client.close()
        server.shared._notify = original_notify
