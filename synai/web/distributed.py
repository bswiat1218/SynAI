from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NewType
from urllib.parse import quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from synai.web.database import MetadataDatabase


LogicalProjectId = NewType("LogicalProjectId", str)
PairedDeviceId = NewType("PairedDeviceId", str)
WorkspaceBindingId = NewType("WorkspaceBindingId", str)
ImmutableSnapshotId = NewType("ImmutableSnapshotId", str)
AgentTaskId = NewType("AgentTaskId", str)
ExecutionTargetId = NewType("ExecutionTargetId", str)
BrokerAllocationId = NewType("BrokerAllocationId", str)
ExecutionClaimId = NewType("ExecutionClaimId", str)
ApprovalReference = NewType("ApprovalReference", str)
ResultReference = NewType("ResultReference", str)
ErrorReference = NewType("ErrorReference", str)

SUPPORTED_PROTOCOL_VERSIONS = frozenset({1})
PAIRING_LIFETIME_SECONDS = 300
DEVICE_CREDENTIAL_LIFETIME_SECONDS = 90 * 24 * 60 * 60
DEVICE_CLOCK_SKEW_SECONDS = 60
DEVICE_NONCE_LIFETIME_SECONDS = 2 * DEVICE_CLOCK_SKEW_SECONDS
_OPAQUE_ID = re.compile(r"[a-f0-9]{32}\Z")
_HEX_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class DistributedError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.status_code = status


@dataclass(frozen=True)
class PairingChallenge:
    challenge_id: str
    secret: str
    expires_at: int
    protocol_versions: tuple[int, ...] = tuple(sorted(SUPPORTED_PROTOCOL_VERSIONS))


@dataclass(frozen=True)
class Enrollment:
    challenge_id: str
    challenge_secret: str
    public_key: str
    protocol_version: int
    capabilities: dict[str, object]
    signature: str


@dataclass(frozen=True)
class DeviceCredential:
    device_id: str
    credential: str
    credential_expires_at: int
    state: str


@dataclass(frozen=True)
class DevicePrincipal:
    device_id: PairedDeviceId
    protocol_version: int
    capabilities: dict[str, object]
    public_key: bytes
    credential_expires_at: int


@dataclass(frozen=True)
class LogicalProject:
    project_id: LogicalProjectId
    display_name: str
    status: str
    created_at: int
    schema_version: int = 1


@dataclass(frozen=True)
class WorkspaceBinding:
    binding_id: WorkspaceBindingId
    project_id: LogicalProjectId
    device_id: PairedDeviceId
    display_name: str
    status: str
    created_at: int
    expires_at: int | None
    schema_version: int = 1


@dataclass(frozen=True)
class DistributedTaskContract:
    schema_version: int
    task_id: AgentTaskId
    project_id: LogicalProjectId
    source_snapshot_id: ImmutableSnapshotId
    source_device_id: PairedDeviceId
    workspace_binding_id: WorkspaceBindingId
    selected_execution_target: ExecutionTargetId | None
    required_capabilities: tuple[str, ...]
    state: str
    execution_claim: ExecutionClaimId | None
    lease_generation: int
    approval_reference: ApprovalReference | None
    result_reference: ResultReference | None
    error_reference: ErrorReference | None


@dataclass(frozen=True)
class ExecutionTargetContract:
    schema_version: int
    target_id: ExecutionTargetId
    target_type: str
    state: str
    capabilities: tuple[str, ...]
    broker_allocation_id: BrokerAllocationId | None


@dataclass(frozen=True)
class ExecutionClaimContract:
    schema_version: int
    claim_id: ExecutionClaimId
    task_id: AgentTaskId
    project_id: LogicalProjectId
    snapshot_id: ImmutableSnapshotId
    target_id: ExecutionTargetId
    broker_allocation_id: BrokerAllocationId | None
    lease_generation: int
    issued_at: int
    expires_at: int
    approval_reference: ApprovalReference | None


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def enrollment_message(
    challenge_id: str,
    challenge_secret: str,
    public_key: bytes,
    protocol_version: int,
    capabilities: dict[str, object],
) -> bytes:
    return b"synai-device-pairing-v1\n" + canonical_json({
        "challenge_id": challenge_id,
        "challenge_secret": challenge_secret,
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "protocol_version": protocol_version,
        "capabilities": capabilities,
    })


def device_request_message(
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
) -> bytes:
    if not method or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("Device request method or path is invalid")
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"synai-device-request-v1\n{method.upper()}\n{quote(path, safe='/:')}\n"
        f"{timestamp}\n{nonce}\n{digest}"
    ).encode("ascii")


class DistributedRegistry:
    """Server-owned logical identities and a device-only authentication namespace."""

    MAX_ACTIVE_PAIRING_CHALLENGES = 128
    MAX_REGISTERED_DEVICES = 256
    MAX_PENDING_DEVICES = 32
    MAX_PROJECTS = 256
    MAX_BINDINGS_PER_PROJECT = 512
    MAX_DEVICE_NONCES = 4096
    MAX_TASKS_PER_PROJECT = 4096

    def __init__(self, database: MetadataDatabase) -> None:
        self.database = database

    def create_pairing_challenge(self, now: int | None = None) -> PairingChallenge:
        timestamp = int(time.time()) if now is None else now
        challenge_id = secrets.token_hex(16)
        secret = secrets.token_urlsafe(32)
        expires_at = timestamp + PAIRING_LIFETIME_SECONDS
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM pairing_challenges WHERE expires_at <= ? OR consumed_at IS NOT NULL",
                    (timestamp,),
                )
                count = connection.execute(
                    "SELECT count(*) AS count FROM pairing_challenges WHERE expires_at > ?",
                    (timestamp,),
                ).fetchone()["count"]
                if count >= self.MAX_ACTIVE_PAIRING_CHALLENGES:
                    raise DistributedError("pairing_capacity", "Pairing challenge capacity is full.", 429)
                connection.execute(
                    "INSERT INTO pairing_challenges"
                    "(challenge_id, secret_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (challenge_id, _digest(secret), timestamp, expires_at),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PairingChallenge(challenge_id, secret, expires_at)

    def enroll(self, request: Enrollment, now: int | None = None) -> DeviceCredential:
        timestamp = int(time.time()) if now is None else now
        _require_id(request.challenge_id, "pairing_invalid")
        if (
            type(request.protocol_version) is not int
            or request.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS
        ):
            raise DistributedError("protocol_unsupported", "Device protocol version is unsupported.", 400)
        capabilities = _validate_capabilities(request.capabilities)
        if (
            not isinstance(request.challenge_secret, str)
            or not 32 <= len(request.challenge_secret) <= 128
            or not isinstance(request.public_key, str)
            or not isinstance(request.signature, str)
        ):
            raise DistributedError("pairing_invalid", "Pairing challenge is invalid or expired.", 410)
        try:
            public_key = base64.b64decode(request.public_key, validate=True)
            signature = base64.b64decode(request.signature, validate=True)
            if len(public_key) != 32 or len(signature) != 64:
                raise ValueError
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                enrollment_message(
                    request.challenge_id,
                    request.challenge_secret,
                    public_key,
                    request.protocol_version,
                    capabilities,
                ),
            )
        except (ValueError, InvalidSignature) as exc:
            raise DistributedError(
                "device_proof_invalid", "Device proof of possession was rejected.", 401,
            ) from exc
        credential = secrets.token_urlsafe(32)
        device_id = secrets.token_hex(16)
        fingerprint = hashlib.sha256(public_key).hexdigest()
        capabilities_json = canonical_json(capabilities).decode("utf-8")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                challenge = connection.execute(
                    "SELECT secret_hash, expires_at, consumed_at FROM pairing_challenges "
                    "WHERE challenge_id = ?",
                    (request.challenge_id,),
                ).fetchone()
                if (
                    challenge is None
                    or challenge["expires_at"] <= timestamp
                    or challenge["consumed_at"] is not None
                    or not hmac.compare_digest(challenge["secret_hash"], _digest(request.challenge_secret))
                ):
                    raise DistributedError(
                        "pairing_replayed_or_expired",
                        "Pairing challenge is invalid, expired, or already used.",
                        410,
                    )
                device_count = connection.execute(
                    "SELECT count(*) AS count FROM paired_devices",
                ).fetchone()["count"]
                pending_count = connection.execute(
                    "SELECT count(*) AS count FROM paired_devices WHERE state = 'pending'",
                ).fetchone()["count"]
                if device_count >= self.MAX_REGISTERED_DEVICES or pending_count >= self.MAX_PENDING_DEVICES:
                    raise DistributedError("device_capacity", "Device enrollment capacity is full.", 429)
                connection.execute(
                    "INSERT INTO paired_devices"
                    "(device_id, schema_version, public_key, key_fingerprint, state, "
                    "protocol_version, capabilities_json, credential_hash, credential_expires_at, "
                    "created_at) VALUES (?, 1, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (
                        device_id, public_key, fingerprint, request.protocol_version,
                        capabilities_json, _digest(credential),
                        timestamp + DEVICE_CREDENTIAL_LIFETIME_SECONDS, timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE pairing_challenges SET consumed_at = ?, device_id = ? "
                    "WHERE challenge_id = ? AND consumed_at IS NULL",
                    (timestamp, device_id, request.challenge_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return DeviceCredential(
            device_id, credential, timestamp + DEVICE_CREDENTIAL_LIFETIME_SECONDS, "pending",
        )

    def authorize_device(self, device_id: str, now: int | None = None) -> dict[str, object]:
        _require_id(device_id, "device_not_found")
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE paired_devices SET state = 'authorized', authorized_at = ? "
                    "WHERE device_id = ? AND state = 'pending' AND credential_expires_at > ?",
                    (timestamp, device_id, timestamp),
                )
                if cursor.rowcount != 1:
                    raise DistributedError("device_not_authorizable", "Device is not awaiting authorization.", 409)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.device_metadata(device_id, now=timestamp)

    def revoke_device(self, device_id: str, now: int | None = None) -> None:
        _require_id(device_id, "device_not_found")
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE paired_devices SET state = 'revoked', revoked_at = ? "
                    "WHERE device_id = ? AND state != 'revoked'",
                    (timestamp, device_id),
                )
                if cursor.rowcount != 1:
                    raise DistributedError("device_not_found", "Device was not found.", 404)
                connection.execute(
                    "UPDATE workspace_bindings SET state = 'revoked', revoked_at = ? "
                    "WHERE device_id = ? AND state != 'revoked'",
                    (timestamp, device_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def device_metadata(self, device_id: str, now: int | None = None) -> dict[str, object]:
        _require_id(device_id, "device_not_found")
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT device_id, state, protocol_version, capabilities_json, "
                "key_fingerprint, created_at, authorized_at, revoked_at, last_seen_at, "
                "credential_expires_at FROM paired_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if row is None:
            raise DistributedError("device_not_found", "Device was not found.", 404)
        return {
            "id": row["device_id"],
            "state": row["state"],
            "protocol_version": row["protocol_version"],
            "capabilities": json.loads(row["capabilities_json"]),
            "key_fingerprint": row["key_fingerprint"],
            "created_at": row["created_at"],
            "authorized_at": row["authorized_at"],
            "revoked_at": row["revoked_at"],
            "last_seen_at": row["last_seen_at"],
            "credential_expires_at": row["credential_expires_at"],
            "connected": (
                row["state"] == "authorized"
                and row["last_seen_at"] is not None
                and timestamp - row["last_seen_at"] <= 120
            ),
        }

    def list_devices(self, now: int | None = None) -> tuple[dict[str, object], ...]:
        with self.database.connect() as connection:
            ids = connection.execute(
                "SELECT device_id FROM paired_devices ORDER BY created_at, device_id LIMIT 256",
            ).fetchall()
        return tuple(self.device_metadata(row["device_id"], now) for row in ids)

    def authenticate_device(
        self,
        credential: str | None,
        device_id: str | None,
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
        method: str,
        path: str,
        body: bytes,
        now: int | None = None,
    ) -> DevicePrincipal:
        current = int(time.time()) if now is None else now
        if not isinstance(device_id, str) or not _OPAQUE_ID.fullmatch(device_id):
            raise DistributedError("device_unauthenticated", "Device authentication is required.", 401)
        if (
            not isinstance(credential, str) or not 32 <= len(credential) <= 128
            or not isinstance(timestamp, str) or not timestamp.isascii()
            or not timestamp.isdigit() or len(timestamp) > 12
            or not isinstance(nonce, str) or not 16 <= len(nonce) <= 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+", nonce)
        ):
            raise DistributedError("device_unauthenticated", "Device authentication is required.", 401)
        issued = int(timestamp)
        if abs(current - issued) > DEVICE_CLOCK_SKEW_SECONDS:
            raise DistributedError("device_request_expired", "Device request timestamp is outside the allowed window.", 401)
        try:
            raw_signature = base64.b64decode(signature or "", validate=True)
        except ValueError as exc:
            raise DistributedError("device_proof_invalid", "Device request proof was rejected.", 401) from exc
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT public_key, state, protocol_version, capabilities_json, "
                "credential_hash, credential_expires_at FROM paired_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if (
            row is None or row["state"] != "authorized"
            or row["credential_expires_at"] <= current
            or not hmac.compare_digest(row["credential_hash"], _digest(credential))
        ):
            raise DistributedError("device_credential_invalid", "Device credential is invalid or revoked.", 401)
        try:
            Ed25519PublicKey.from_public_bytes(row["public_key"]).verify(
                raw_signature,
                device_request_message(method, path, issued, nonce, body),
            )
        except (ValueError, InvalidSignature) as exc:
            raise DistributedError("device_proof_invalid", "Device request proof was rejected.", 401) from exc
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM device_request_nonces WHERE expires_at <= ?", (current,))
                nonce_count = connection.execute(
                    "SELECT count(*) AS count FROM device_request_nonces WHERE device_id = ?",
                    (device_id,),
                ).fetchone()["count"]
                if nonce_count >= self.MAX_DEVICE_NONCES:
                    raise DistributedError("device_request_capacity", "Device request rate capacity is full.", 429)
                connection.execute(
                    "INSERT INTO device_request_nonces(device_id, nonce, expires_at) VALUES (?, ?, ?)",
                    (device_id, nonce, issued + DEVICE_NONCE_LIFETIME_SECONDS),
                )
                connection.execute(
                    "UPDATE paired_devices SET last_seen_at = ? WHERE device_id = ? "
                    "AND state = 'authorized' AND credential_expires_at > ?",
                    (current, device_id, current),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise DistributedError("device_credential_invalid", "Device credential is invalid or revoked.", 401)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DistributedError("device_request_replayed", "Device request nonce was already used.", 409) from exc
            except BaseException:
                connection.rollback()
                raise
        return DevicePrincipal(
            PairedDeviceId(device_id),
            row["protocol_version"],
            json.loads(row["capabilities_json"]),
            row["public_key"],
            row["credential_expires_at"],
        )

    def rotate_device_credential(self, principal: DevicePrincipal, now: int | None = None) -> DeviceCredential:
        timestamp = int(time.time()) if now is None else now
        raw = secrets.token_urlsafe(32)
        expires = timestamp + DEVICE_CREDENTIAL_LIFETIME_SECONDS
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE paired_devices SET credential_hash = ?, credential_expires_at = ?, "
                    "credential_generation = credential_generation + 1 "
                    "WHERE device_id = ? AND state = 'authorized' AND credential_expires_at > ?",
                    (_digest(raw), expires, str(principal.device_id), timestamp),
                )
                if cursor.rowcount != 1:
                    raise DistributedError("device_credential_invalid", "Device credential is invalid or revoked.", 401)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return DeviceCredential(str(principal.device_id), raw, expires, "authorized")

    def create_project(
        self, display_name: str, registration_key: str, now: int | None = None,
    ) -> LogicalProject:
        timestamp = int(time.time()) if now is None else now
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
            raise DistributedError("invalid_project_name", "Project name must contain 1 to 128 characters.", 422)
        if (
            not isinstance(registration_key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", registration_key)
        ):
            raise DistributedError("invalid_idempotency_key", "Project registration key is invalid.", 422)
        name = display_name.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise DistributedError("invalid_project_name", "Project name contains unsupported control characters.", 422)
        key_hash = _digest(registration_key)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM logical_projects WHERE registration_key_hash = ?",
                    (key_hash,),
                ).fetchone()
                if existing is not None:
                    if existing["display_name"] != name:
                        raise DistributedError("idempotency_conflict", "Project registration key was already used.", 409)
                    connection.commit()
                    return _logical_project(existing)
                count = connection.execute(
                    "SELECT count(*) AS count FROM logical_projects",
                ).fetchone()["count"]
                if count >= self.MAX_PROJECTS:
                    raise DistributedError("project_capacity", "Logical project capacity is full.", 429)
                project_id = secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO logical_projects(project_id, schema_version, registration_key_hash, "
                    "display_name, created_at, status) VALUES (?, 1, ?, ?, ?, 'active')",
                    (project_id, key_hash, name, timestamp),
                )
                row = connection.execute(
                    "SELECT * FROM logical_projects WHERE project_id = ?", (project_id,),
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return _logical_project(row)

    def list_projects(self) -> tuple[LogicalProject, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM logical_projects ORDER BY created_at, project_id LIMIT 256",
            ).fetchall()
        return tuple(_logical_project(row) for row in rows)

    def get_project(self, project_id: str) -> LogicalProject:
        _require_id(project_id, "project_not_found")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM logical_projects WHERE project_id = ?", (project_id,),
            ).fetchone()
        if row is None:
            raise DistributedError("project_not_found", "Logical project was not found.", 404)
        return _logical_project(row)

    def create_binding(
        self,
        project_id: str,
        device_id: str,
        display_name: str,
        *,
        expires_at: int | None = None,
        now: int | None = None,
    ) -> WorkspaceBinding:
        timestamp = int(time.time()) if now is None else now
        self.get_project(project_id)
        _require_id(device_id, "device_not_found")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
            raise DistributedError("invalid_binding_name", "Workspace label must contain 1 to 128 characters.", 422)
        if expires_at is not None and (
            type(expires_at) is not int or expires_at <= timestamp
            or expires_at > timestamp + 365 * 24 * 60 * 60
        ):
            raise DistributedError("invalid_binding_expiry", "Workspace binding expiry is outside supported bounds.", 422)
        name = display_name.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise DistributedError("invalid_binding_name", "Workspace label contains unsupported control characters.", 422)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT count(*) AS count FROM workspace_bindings WHERE project_id = ?",
                    (project_id,),
                ).fetchone()["count"]
                if existing >= self.MAX_BINDINGS_PER_PROJECT:
                    raise DistributedError("binding_capacity", "Project workspace binding capacity is full.", 429)
                device = connection.execute(
                    "SELECT state FROM paired_devices WHERE device_id = ?", (device_id,),
                ).fetchone()
                if device is None or device["state"] != "authorized":
                    raise DistributedError("device_not_authorized", "An authorized device is required.", 403)
                binding_id = secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO workspace_bindings"
                    "(binding_id, schema_version, project_id, device_id, display_name, state, "
                    "created_at, expires_at) VALUES (?, 1, ?, ?, ?, 'active', ?, ?)",
                    (binding_id, project_id, device_id, name, timestamp, expires_at),
                )
                row = connection.execute(
                    "SELECT * FROM workspace_bindings WHERE binding_id = ?", (binding_id,),
                ).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DistributedError("binding_duplicate", "Workspace binding already exists.", 409) from exc
            except BaseException:
                connection.rollback()
                raise
        return _workspace_binding(row, timestamp)

    def list_bindings(
        self, project_id: str, now: int | None = None,
    ) -> tuple[WorkspaceBinding, ...]:
        self.get_project(project_id)
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workspace_bindings WHERE project_id = ? "
                "ORDER BY created_at, binding_id LIMIT 512",
                (project_id,),
            ).fetchall()
        return tuple(_workspace_binding(row, timestamp) for row in rows)

    def require_binding(
        self,
        project_id: str,
        device_id: str,
        binding_id: str,
        now: int | None = None,
    ) -> WorkspaceBinding:
        timestamp = int(time.time()) if now is None else now
        self.get_project(project_id)
        _require_id(device_id, "binding_not_found")
        _require_id(binding_id, "binding_not_found")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT b.*, d.state AS device_state FROM workspace_bindings b "
                "JOIN paired_devices d USING(device_id) WHERE b.binding_id = ? "
                "AND b.project_id = ? AND b.device_id = ?",
                (binding_id, project_id, device_id),
            ).fetchone()
        if row is None:
            raise DistributedError("binding_not_found", "Workspace binding was not found.", 404)
        binding = _workspace_binding(row, timestamp)
        if binding.status != "active":
            raise DistributedError("binding_stale", "Workspace binding is stale or revoked.", 409)
        if row["device_state"] != "authorized":
            raise DistributedError("device_not_authorized", "Device is not authorized.", 403)
        return binding

    def revoke_binding(self, project_id: str, binding_id: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        self.get_project(project_id)
        _require_id(binding_id, "binding_not_found")
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE workspace_bindings SET state = 'revoked', revoked_at = ? "
                "WHERE binding_id = ? AND project_id = ? AND state != 'revoked'",
                (timestamp, binding_id, project_id),
            )
        if cursor.rowcount != 1:
            raise DistributedError("binding_not_found", "Workspace binding was not found.", 404)

    def create_disabled_task_contract(
        self,
        project_id: str,
        snapshot_id: str,
        required_capabilities: tuple[str, ...] = (),
        now: int | None = None,
    ) -> DistributedTaskContract:
        """Persist an identity-only task contract; it cannot be claimed or executed."""
        timestamp = int(time.time()) if now is None else now
        self.get_project(project_id)
        _require_id(snapshot_id, "snapshot_not_found")
        if (
            not isinstance(required_capabilities, tuple)
            or len(required_capabilities) > 32
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
                for value in required_capabilities
            )
            or len(set(required_capabilities)) != len(required_capabilities)
        ):
            raise DistributedError("task_contract_invalid", "Task capability requirements are invalid.", 422)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                snapshot = connection.execute(
                    "SELECT * FROM immutable_snapshots WHERE snapshot_id = ? "
                    "AND project_id = ? AND state = 'available' AND expires_at > ?",
                    (snapshot_id, project_id, timestamp),
                ).fetchone()
                if snapshot is None:
                    raise DistributedError("snapshot_not_found", "Available project snapshot was not found.", 404)
                task_count = connection.execute(
                    "SELECT count(*) AS count FROM distributed_tasks WHERE project_id = ?",
                    (project_id,),
                ).fetchone()["count"]
                if task_count >= self.MAX_TASKS_PER_PROJECT:
                    raise DistributedError("task_capacity", "Distributed task contract capacity is full.", 429)
                binding = connection.execute(
                    "SELECT state, expires_at FROM workspace_bindings WHERE binding_id = ? "
                    "AND project_id = ? AND device_id = ?",
                    (snapshot["binding_id"], project_id, snapshot["device_id"]),
                ).fetchone()
                device = connection.execute(
                    "SELECT state FROM paired_devices WHERE device_id = ?",
                    (snapshot["device_id"],),
                ).fetchone()
                if (
                    binding is None or binding["state"] != "active"
                    or binding["expires_at"] is not None and binding["expires_at"] <= timestamp
                    or device is None or device["state"] != "authorized"
                ):
                    raise DistributedError("task_provenance_stale", "Snapshot device or workspace binding is stale.", 409)
                task_id = secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO distributed_tasks(task_id, schema_version, project_id, snapshot_id, "
                    "device_id, binding_id, state, execution_target, required_capabilities_json, "
                    "execution_claim, fencing_generation, approval_reference, result_reference, "
                    "error_reference, created_at, updated_at) "
                    "VALUES (?, 1, ?, ?, ?, ?, 'not_enabled', '', ?, NULL, 0, NULL, NULL, NULL, ?, ?)",
                    (
                        task_id, project_id, snapshot_id, snapshot["device_id"],
                        snapshot["binding_id"], canonical_json(sorted(required_capabilities)).decode("utf-8"),
                        timestamp, timestamp,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return DistributedTaskContract(
            schema_version=1,
            task_id=AgentTaskId(task_id),
            project_id=LogicalProjectId(project_id),
            source_snapshot_id=ImmutableSnapshotId(snapshot_id),
            source_device_id=PairedDeviceId(snapshot["device_id"]),
            workspace_binding_id=WorkspaceBindingId(snapshot["binding_id"]),
            selected_execution_target=None,
            required_capabilities=tuple(sorted(required_capabilities)),
            state="not_enabled",
            execution_claim=None,
            lease_generation=0,
            approval_reference=None,
            result_reference=None,
            error_reference=None,
        )

    def authorize_memory_association_preview(
        self,
        legacy_identity: str,
        project_id: str,
        provenance: dict[str, object],
        now: int | None = None,
    ) -> dict[str, object]:
        self.get_project(project_id)
        if not isinstance(legacy_identity, str) or not _HEX_DIGEST.fullmatch(legacy_identity):
            raise DistributedError("memory_identity_invalid", "Legacy memory identity is invalid.", 422)
        if (
            not isinstance(provenance, dict) or len(provenance) > 8
            or any(
                not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                for key in provenance
            )
            or any(
                value is not None
                and type(value) not in {str, int, bool}
                for value in provenance.values()
            )
            or any(isinstance(value, str) and len(value) > 512 for value in provenance.values())
            or not isinstance(provenance.get("source"), str)
            or type(provenance.get("source_version")) is not int
            or not 1 <= provenance["source_version"] <= 255
            or "backup_sha256" in provenance
            and (
                not isinstance(provenance["backup_sha256"], str)
                or not _HEX_DIGEST.fullmatch(provenance["backup_sha256"])
            )
            or len(canonical_json(provenance)) > 2048
        ):
            raise DistributedError("memory_provenance_invalid", "Memory association provenance is invalid.", 422)
        return {
            "association_id": secrets.token_hex(16),
            "schema_version": 1,
            "legacy_identity": legacy_identity,
            "project_id": project_id,
            "status": "preview_only",
            "provenance_validated": True,
            "record_count": None,
            "migration_enabled": False,
        }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_id(value: object, code: str) -> None:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise DistributedError(code, "Identifier was not found.", 404)


def _validate_capabilities(value: object) -> dict[str, object]:
    allowed = {"agent_version", "platform", "architecture", "features"}
    if not isinstance(value, dict) or set(value) != allowed:
        raise DistributedError("capabilities_invalid", "Device capabilities do not match the supported schema.", 422)
    for key in ("agent_version", "platform", "architecture"):
        item = value[key]
        if (
            not isinstance(item, str) or not item or len(item) > 64
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in item)
        ):
            raise DistributedError("capabilities_invalid", "Device capabilities do not match the supported schema.", 422)
    features = value["features"]
    if (
        not isinstance(features, list) or len(features) > 32
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item) for item in features)
        or len(set(features)) != len(features)
    ):
        raise DistributedError("capabilities_invalid", "Device capabilities do not match the supported schema.", 422)
    if not set(features).issubset({"snapshot-v1", "status-v1"}):
        raise DistributedError("capabilities_invalid", "Device advertised an unsupported capability.", 422)
    if len(canonical_json(value)) > 4096:
        raise DistributedError("capabilities_invalid", "Device capabilities exceed the supported size.", 422)
    return {
        "agent_version": value["agent_version"],
        "platform": value["platform"],
        "architecture": value["architecture"],
        "features": sorted(features),
    }


def _logical_project(row: sqlite3.Row) -> LogicalProject:
    return LogicalProject(
        LogicalProjectId(row["project_id"]), row["display_name"], row["status"],
        row["created_at"], row["schema_version"],
    )


def _workspace_binding(row: sqlite3.Row, now: int) -> WorkspaceBinding:
    status = row["state"]
    if status == "active" and row["expires_at"] is not None and row["expires_at"] <= now:
        status = "stale"
    return WorkspaceBinding(
        WorkspaceBindingId(row["binding_id"]),
        LogicalProjectId(row["project_id"]),
        PairedDeviceId(row["device_id"]),
        row["display_name"],
        status,
        row["created_at"],
        row["expires_at"],
        row["schema_version"],
    )
