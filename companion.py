"""Pairing and authorization primitives for the Rainette LAN companion.

This module deliberately contains no HTTP or UI code.  The desktop listener
uses it to turn a short-lived QR invitation into a revocable device credential
and to mint opaque, device-bound audio-relay grants.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import secrets
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from filelock import FileLock


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _Invitation:
    expires_at: float
    used: bool = False


@dataclass
class _PairingRequest:
    request_id: str
    invitation_hash: str
    device_name: str
    public_key: str
    status: str
    created_at: float
    status_changed_at: float
    encrypted_device_token: str | None = None
    device_id: str | None = None
    claim_expires_at: float | None = None


@dataclass
class _Device:
    name: str
    token_hash: str
    revoked: bool = False


@dataclass
class _UnclaimedDevice:
    request_id: str
    device_id: str
    expires_at: float


@dataclass
class _RelayGrant:
    device_id: str
    upstream_url: str
    expires_at: float


@dataclass(frozen=True)
class CompanionCertificate:
    cert_path: Path
    key_path: Path
    fingerprint_sha256: str
    ssl_context: ssl.SSLContext


def ensure_tls_certificate(directory: Path) -> CompanionCertificate:
    """Create (once) the self-signed LAN certificate pinned during QR pairing."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "companion-cert.pem"
    key_path = directory / "companion-key.pem"
    if not cert_path.is_file() or not key_path.is_file():
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rainette Music Companion")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(private_key, hashes.SHA256())
        )
        key_path.write_bytes(private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ))
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return CompanionCertificate(
        cert_path=cert_path,
        key_path=key_path,
        fingerprint_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
        ssl_context=context,
    )


class CompanionRegistry:
    """In-memory authorization registry used by the first companion listener.

    Tokens are generated once and only their SHA-256 digests are retained.
    Persistence is intentionally injected by the server layer later; keeping
    the security state isolated makes expiry and revocation testable.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        storage_path: Path | None = None,
        claim_ttl_s: int = 120,
    ) -> None:
        if claim_ttl_s <= 0:
            raise ValueError("claim_ttl_s must be positive")
        self._now = now
        self._claim_ttl_s = claim_ttl_s
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self._file_lock: FileLock | None = None
        if self.storage_path is not None:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_lock = FileLock(str(self.storage_path) + ".lock")
        self._lock = threading.RLock()
        self._invitations: dict[str, _Invitation] = {}
        self._requests: dict[str, _PairingRequest] = {}
        self._devices: dict[str, _Device] = {}
        self._unclaimed: dict[str, _UnclaimedDevice] = {}
        self._relay_grants: dict[str, _RelayGrant] = {}
        self._load_devices()

    @staticmethod
    def _copy_devices(devices: dict[str, _Device]) -> dict[str, _Device]:
        return {
            device_id: _Device(name=device.name, token_hash=device.token_hash, revoked=device.revoked)
            for device_id, device in devices.items()
        }

    def _read_state(self) -> tuple[dict[str, _Device], dict[str, _UnclaimedDevice]]:
        if self.storage_path is None or not self.storage_path.is_file():
            return {}, {}
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            devices: dict[str, _Device] = {}
            for item in payload.get("devices", []):
                device_id = str(item["device_id"])
                devices[device_id] = _Device(
                    name=str(item["name"]),
                    token_hash=str(item["token_hash"]),
                    revoked=bool(item.get("revoked", False)),
                )
            claims: dict[str, _UnclaimedDevice] = {}
            for item in payload.get("claims", []):
                request_id = str(item["request_id"])
                claims[request_id] = _UnclaimedDevice(
                    request_id=request_id,
                    device_id=str(item["device_id"]),
                    expires_at=float(item.get("expires_at", item.get("claim_expires_at"))),
                )
            return devices, claims
        except (OSError, ValueError, KeyError, TypeError):
            # A damaged optional state file must not prevent Rainette starting.
            return {}, {}

    def _read_devices(self) -> dict[str, _Device]:
        return self._read_state()[0]

    def _load_devices(self) -> None:
        if self._file_lock is None:
            self._devices, self._unclaimed = self._read_state()
            return
        with self._file_lock:
            self._devices, self._unclaimed = self._read_state()

    def _persist_devices(self, devices: dict[str, _Device] | None = None) -> None:
        if self.storage_path is None:
            return
        snapshot = self._devices if devices is None else devices
        _, persisted_claims = self._read_state()
        persisted_claims.update({
            request_id: _UnclaimedDevice(
                request_id=request_id,
                device_id=str(request.device_id),
                expires_at=float(request.claim_expires_at),
            )
            for request_id, request in self._requests.items()
            if request.status == "approved" and request.device_id and request.claim_expires_at is not None
        })
        self._write_state(snapshot, persisted_claims)
        self._unclaimed = persisted_claims

    def _write_state(
        self,
        devices: dict[str, _Device],
        claims: dict[str, _UnclaimedDevice],
    ) -> None:
        if self.storage_path is None:
            return
        payload = {
            "version": 2,
            "devices": [
                {
                    "device_id": device_id,
                    "name": device.name,
                    "token_hash": device.token_hash,
                    "revoked": device.revoked,
                }
                for device_id, device in devices.items()
            ],
            "claims": [
                {
                    "request_id": claim.request_id,
                    "device_id": claim.device_id,
                    "expires_at": claim.expires_at,
                }
                for claim in claims.values()
            ],
        }
        temporary = self.storage_path.with_name(f".{self.storage_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.storage_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _mutate_devices(self, mutation: Callable[[dict[str, _Device]], object]) -> object:
        """Persist a candidate snapshot before publishing it in memory.

        The process lock serializes registry instances using the same file and
        the reload prevents one process from overwriting another's devices.
        """
        if self._file_lock is None:
            candidate = self._copy_devices(self._devices)
            result = mutation(candidate)
            self._persist_devices(candidate)
            self._devices = candidate
            return result
        with self._file_lock:
            candidate = self._read_devices()
            result = mutation(candidate)
            self._persist_devices(candidate)
            self._devices = candidate
            return result

    def _refresh_and_cleanup(self) -> None:
        """Refresh shared security state and expire unacknowledged claims.

        Device removal and claim removal are written in one atomic snapshot so
        a crash or second Rainette process cannot leave an orphan credential.
        Caller must hold ``self._lock``.
        """
        now = self._now()
        if self._file_lock is None:
            devices = self._devices
            claims = {
                request_id: _UnclaimedDevice(
                    request_id=request_id,
                    device_id=str(request.device_id),
                    expires_at=float(request.claim_expires_at),
                )
                for request_id, request in self._requests.items()
                if request.status == "approved" and request.device_id and request.claim_expires_at is not None
            }
        else:
            with self._file_lock:
                devices, claims = self._read_state()
                self._devices = devices
                self._unclaimed = claims
                for request_id in [
                    key
                    for key, request in self._requests.items()
                    if request.status == "approved" and key not in claims
                ]:
                    self._requests.pop(request_id, None)
                expired = [
                    claim for claim in claims.values() if claim.expires_at <= now
                ]
                if expired:
                    for claim in expired:
                        devices.pop(claim.device_id, None)
                        claims.pop(claim.request_id, None)
                        local = self._requests.get(claim.request_id)
                        if local is not None:
                            local.status = "expired"
                            local.status_changed_at = now
                            local.encrypted_device_token = None
                    self._write_state(devices, claims)
                self._devices = devices
                self._unclaimed = claims
                return

        expired = [
            claim for claim in claims.values() if claim.expires_at <= now
        ]
        for claim in expired:
            devices.pop(claim.device_id, None)
            request = self._requests.get(claim.request_id)
            if request is not None:
                request.status = "expired"
                request.status_changed_at = now
                request.encrypted_device_token = None

    @staticmethod
    def _load_phone_public_key(value: str) -> rsa.RSAPublicKey:
        encoded = str(value or "").strip().encode("ascii", "strict")
        try:
            if encoded.startswith(b"-----BEGIN"):
                key = serialization.load_pem_public_key(encoded)
            else:
                key = serialization.load_der_public_key(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError) as exc:
            raise ValueError("a valid RSA public key is required") from exc
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("a 2048-bit RSA public key is required")
        return key

    def create_invitation(self, *, ttl_s: int = 300) -> dict[str, object]:
        with self._lock:
            if ttl_s <= 0:
                raise ValueError("ttl_s must be positive")
            token = secrets.token_urlsafe(32)
            expires_at = self._now() + ttl_s
            self._invitations[_digest(token)] = _Invitation(expires_at=expires_at)
            return {"token": token, "expires_at": expires_at}

    def request_pairing(self, invitation_token: str, device_name: str, public_key: str) -> dict[str, str]:
        with self._lock:
            invitation_hash = _digest(invitation_token)
            invitation = self._invitations.get(invitation_hash)
            if invitation is None or invitation.used or invitation.expires_at <= self._now():
                raise ValueError("pairing invitation is expired or invalid")
            if not str(device_name).strip() or not str(public_key).strip():
                raise ValueError("device name and public key are required")
            self._load_phone_public_key(public_key)
            # Reserve the invitation only after the complete request validates.
            invitation.used = True
            request_id = uuid.uuid4().hex
            now = self._now()
            self._requests[request_id] = _PairingRequest(
                request_id=request_id,
                invitation_hash=invitation_hash,
                device_name=str(device_name).strip(),
                public_key=str(public_key).strip(),
                status="pending",
                created_at=now,
                status_changed_at=now,
            )
            return {"request_id": request_id, "status": "pending"}

    def pending_requests(self) -> list[dict[str, str]]:
        """Return pending pairing metadata safe to expose to the desktop UI."""
        with self._lock:
            self._refresh_and_cleanup()
            now = self._now()
            expired = [
                request
                for request in self._requests.values()
                if (
                    self._invitations.get(request.invitation_hash) is None
                    or self._invitations[request.invitation_hash].expires_at <= now
                )
            ]
            for request in expired:
                if request.status == "pending":
                    request.status = "expired"
                    request.status_changed_at = now
            return [
                {
                    "request_id": request.request_id,
                    "device_name": request.device_name,
                    "status": request.status,
                }
                for request in self._requests.values()
                if request.status == "pending"
            ]

    def reject(self, request_id: str) -> bool:
        """Reject a pending request while preserving a phone-readable result."""
        with self._lock:
            self._refresh_and_cleanup()
            request = self._requests.get(str(request_id))
            if request is None or request.status != "pending":
                return False
            request.status = "rejected"
            request.status_changed_at = self._now()
            return True

    def approve(self, request_id: str) -> dict[str, str]:
        with self._lock:
            self._refresh_and_cleanup()
            request = self._requests.get(request_id)
            if request is None or request.status != "pending":
                raise ValueError("pairing request was not found")
            invitation = self._invitations.get(request.invitation_hash)
            if invitation is None or invitation.expires_at <= self._now():
                request.status = "expired"
                raise ValueError("pairing invitation is expired or invalid")
            device_id = uuid.uuid4().hex
            device_token = secrets.token_urlsafe(48)
            public_key = self._load_phone_public_key(request.public_key)
            encrypted = public_key.encrypt(
                device_token.encode("utf-8"),
                padding.OAEP(
                    # Android Keystore uses SHA-1 for OAEP's MGF1 digest on
                    # API levels below 35.  The OAEP message digest remains
                    # SHA-256; using SHA-1 only for the mask-generation
                    # function keeps the ciphertext interoperable with every
                    # Android version Rainette supports (API 23+).
                    mgf=padding.MGF1(algorithm=hashes.SHA1()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            device = _Device(name=request.device_name, token_hash=_digest(device_token))

            def add_device(devices: dict[str, _Device]) -> None:
                devices[device_id] = device

            approved_at = self._now()
            previous = (
                request.status, request.status_changed_at, request.device_id,
                request.encrypted_device_token, request.claim_expires_at,
            )
            request.status = "approved"
            request.status_changed_at = approved_at
            request.device_id = device_id
            request.encrypted_device_token = base64.b64encode(encrypted).decode("ascii")
            request.claim_expires_at = approved_at + self._claim_ttl_s
            try:
                self._mutate_devices(add_device)
            except Exception:
                (
                    request.status, request.status_changed_at, request.device_id,
                    request.encrypted_device_token, request.claim_expires_at,
                ) = previous
                raise
            return {"device_id": device_id, "device_name": request.device_name, "status": "approved"}

    def pairing_result(self, request_id: str, invitation_token: str) -> dict[str, str] | None:
        """Return a result only to the phone proving possession of its invite.

        Approved results remain available until the authenticated phone
        acknowledges durable credential storage or the claim TTL expires.
        """
        with self._lock:
            self._refresh_and_cleanup()
            request = self._requests.get(str(request_id))
            supplied = _digest(str(invitation_token or ""))
            if request is None or not hmac.compare_digest(request.invitation_hash, supplied):
                return None
            now = self._now()
            invitation = self._invitations.get(request.invitation_hash)
            if request.status == "pending" and (invitation is None or invitation.expires_at <= now):
                request.status = "expired"
                request.status_changed_at = now
            if request.status == "approved":
                return {
                    "status": "approved",
                    "device_id": str(request.device_id),
                    "encrypted_device_token": str(request.encrypted_device_token),
                }
            return {"status": request.status}

    def acknowledge_pairing(self, request_id: str, device_id: str) -> bool:
        """Remove an approved claim after its persisted device authenticates.

        Persisted claims are authoritative here so a phone can finish the
        acknowledgement handshake after the desktop process restarts.
        """
        with self._lock:
            self._refresh_and_cleanup()
            supplied_request_id = str(request_id)
            supplied_device_id = str(device_id)
            request = self._requests.get(supplied_request_id)
            if self._file_lock is None:
                if (
                    request is None
                    or request.status != "approved"
                    or not hmac.compare_digest(str(request.device_id), supplied_device_id)
                ):
                    return False
                self._requests.pop(request.request_id, None)
            else:
                with self._file_lock:
                    devices, claims = self._read_state()
                    persisted = claims.get(supplied_request_id)
                    device = devices.get(supplied_device_id)
                    if persisted is None or not hmac.compare_digest(
                        str(persisted.device_id), supplied_device_id
                    ) or device is None or device.revoked:
                        return False
                    claims.pop(supplied_request_id, None)
                    self._write_state(devices, claims)
                    self._devices = devices
                    self._unclaimed = claims
                    self._requests.pop(supplied_request_id, None)
            if request is not None:
                self._invitations.pop(request.invitation_hash, None)
            return True

    def authorize(self, device_token: str) -> bool:
        with self._lock:
            self._refresh_and_cleanup()
            supplied_hash = _digest(str(device_token or ""))
            return any(
                not device.revoked and hmac.compare_digest(device.token_hash, supplied_hash)
                for device in self._devices.values()
            )

    def device_id_for_token(self, device_token: str) -> str | None:
        with self._lock:
            self._refresh_and_cleanup()
            supplied_hash = _digest(str(device_token or ""))
            for device_id, device in self._devices.items():
                if not device.revoked and hmac.compare_digest(device.token_hash, supplied_hash):
                    return device_id
            return None

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            self._refresh_and_cleanup()
            def revoke_device(devices: dict[str, _Device]) -> bool:
                device = devices.get(device_id)
                if device is None:
                    return False
                device.revoked = True
                return True

            return bool(self._mutate_devices(revoke_device))

    def devices(self) -> list[dict[str, object]]:
        """Return device metadata without exposing stored credential digests."""
        with self._lock:
            self._refresh_and_cleanup()
            return [
                {"device_id": device_id, "name": device.name, "revoked": device.revoked}
                for device_id, device in self._devices.items()
            ]

    def create_relay_grant(self, device_id: str, upstream_url: str, *, ttl_s: int = 120) -> dict[str, object]:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.revoked:
                raise ValueError("device is not authorized")
            if ttl_s <= 0 or not str(upstream_url).strip():
                raise ValueError("relay grant requires a URL and positive ttl")
            token = secrets.token_urlsafe(32)
            expires_at = self._now() + ttl_s
            self._relay_grants[_digest(token)] = _RelayGrant(device_id, str(upstream_url), expires_at)
            return {"token": token, "expires_at": expires_at}

    def resolve_relay(self, grant_token: str, device_token: str) -> str | None:
        with self._lock:
            device_id = self.device_id_for_token(device_token)
            grant = self._relay_grants.get(_digest(str(grant_token or "")))
            if device_id is None or grant is None or grant.expires_at <= self._now() or grant.device_id != device_id:
                return None
            return grant.upstream_url
