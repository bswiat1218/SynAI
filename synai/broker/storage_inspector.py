from __future__ import annotations

import json
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from synai.broker.config import StorageRole


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024
_HEX_256 = re.compile(r"[a-f0-9]{64}\Z")
_EPOCH = re.compile(r"[a-f0-9]{32}\Z")
_ROLE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_REQUEST_KEYS = {
    "schema_version", "message_type", "role_id", "challenge",
    "inventory_generation",
}
_RESPONSE_COMMON_KEYS = {
    "schema_version", "message_type", "role_id", "challenge", "status",
    "helper_boot_epoch", "inventory_generation",
}
_OBSERVATION_KEYS = _RESPONSE_COMMON_KEYS | {
    "observed_at", "filesystem_uuid", "mount_target", "mount_id",
    "filesystem_type", "quota_mode", "quota_configuration_enabled", "project_id",
    "inheritance_enabled", "byte_limit", "inode_limit", "bytes_used",
    "inodes_used", "filesystem_bytes", "filesystem_bytes_available",
    "filesystem_inodes", "filesystem_inodes_available", "owner_uid", "owner_gid",
}
_ERROR_KEYS = _RESPONSE_COMMON_KEYS | {"error_code"}


class InspectorFailure(RuntimeError):
    state = "invalid"


class InspectorUnavailable(InspectorFailure):
    state = "unavailable"


class InspectorInvalid(InspectorFailure):
    state = "invalid"


class InspectorStale(InspectorFailure):
    state = "stale"


class InspectorInconsistent(InspectorFailure):
    state = "inconsistent"


@dataclass(frozen=True)
class StorageObservation:
    role_id: str
    helper_boot_epoch: str
    inventory_generation: str
    observed_at: int
    filesystem_uuid: str
    mount_target: str
    mount_id: int
    filesystem_type: str
    quota_mode: str
    quota_configuration_enabled: bool
    project_id: int
    inheritance_enabled: bool
    byte_limit: int
    inode_limit: int
    bytes_used: int
    inodes_used: int
    filesystem_bytes: int
    filesystem_bytes_available: int
    filesystem_inodes: int
    filesystem_inodes_available: int
    owner_uid: int
    owner_gid: int


@dataclass(frozen=True)
class StorageInspectionRequest:
    schema_version: int
    message_type: str
    role_id: str
    challenge: str
    inventory_generation: str

    @classmethod
    def issue(cls, role_id: str, generation: str) -> StorageInspectionRequest:
        return cls(
            schema_version=PROTOCOL_VERSION,
            message_type="inspect_storage_role",
            role_id=role_id,
            challenge=secrets.token_hex(32),
            inventory_generation=generation,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        *,
        approved_roles: frozenset[str],
        expected_generation: str,
    ) -> StorageInspectionRequest:
        if (
            not isinstance(value, dict)
            or set(value) != _REQUEST_KEYS
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != PROTOCOL_VERSION
            or value.get("message_type") != "inspect_storage_role"
            or not isinstance(value.get("role_id"), str)
            or not _ROLE.fullmatch(value["role_id"])
            or value["role_id"] not in approved_roles
            or not isinstance(value.get("challenge"), str)
            or not _HEX_256.fullmatch(value["challenge"])
            or value.get("inventory_generation") != expected_generation
        ):
            raise InspectorInvalid("Quota inspection request is invalid or unauthorized.")
        return cls(
            schema_version=value["schema_version"],
            message_type=value["message_type"],
            role_id=value["role_id"],
            challenge=value["challenge"],
            inventory_generation=value["inventory_generation"],
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "message_type": self.message_type,
            "role_id": self.role_id,
            "challenge": self.challenge,
            "inventory_generation": self.inventory_generation,
        }


class StorageInspector(Protocol):
    def inspect(self, role_id: str) -> StorageObservation: ...


class UnixStorageInspectorClient:
    """Bounded local client for the future fixed-inventory inspector service."""

    def __init__(
        self,
        socket_path: Path | None,
        roles: tuple[StorageRole, ...],
        *,
        inspector_uid: int,
        inspector_gid: int,
        timeout_seconds: float,
        max_age_seconds: int,
        inventory_generation: str,
    ) -> None:
        self.socket_path = socket_path
        self.roles = frozenset(role.role_id for role in roles)
        self.inspector_uid = inspector_uid
        self.inspector_gid = inspector_gid
        self.timeout_seconds = timeout_seconds
        self.max_age_seconds = max_age_seconds
        self.inventory_generation = inventory_generation
        self._boot_epoch: str | None = None
        self._challenges: set[str] = set()
        self._challenge_lock = threading.Lock()
        self._epoch_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.socket_path is not None and bool(self.roles)

    def inspect(self, role_id: str) -> StorageObservation:
        if not isinstance(role_id, str) or not _ROLE.fullmatch(role_id) or role_id not in self.roles:
            raise InspectorInvalid("Storage role is not in the operator-defined inventory.")
        if self.socket_path is None:
            raise InspectorUnavailable("Quota inspector socket is not configured.")

        last_error: InspectorUnavailable | None = None
        for attempt in range(2):
            typed_request = StorageInspectionRequest.issue(
                role_id, self.inventory_generation,
            )
            challenge = typed_request.challenge
            with self._challenge_lock:
                if challenge in self._challenges:
                    raise InspectorInvalid("A cryptographic inspection challenge was reused.")
                self._challenges.add(challenge)
                if len(self._challenges) > 4096:
                    self._challenges = {challenge}
            request = typed_request.to_wire()
            try:
                response = self._exchange(request)
                return self._parse_response(response, role_id, challenge)
            except InspectorUnavailable as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.025)
        raise last_error or InspectorUnavailable("Quota inspector is unavailable.")

    def _exchange(self, request: dict[str, object]) -> object:
        assert self.socket_path is not None
        try:
            self._verify_socket_path(self.socket_path)
            payload = json.dumps(
                request, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            if len(payload) > MAX_FRAME_BYTES:
                raise InspectorInvalid("Quota inspector request exceeds its frame limit.")
            deadline = time.monotonic() + self.timeout_seconds
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.connect(str(self.socket_path))
                self._verify_peer(connection)
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.sendall(struct.pack("!I", len(payload)) + payload)
                header = self._receive_exact(connection, 4, deadline)
                frame_size = struct.unpack("!I", header)[0]
                if not 1 <= frame_size <= MAX_FRAME_BYTES:
                    raise InspectorInvalid("Quota inspector response frame size is invalid.")
                frame = self._receive_exact(connection, frame_size, deadline)
            try:
                return json.loads(
                    frame,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            except (
                json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError,
            ) as exc:
                raise InspectorInvalid("Quota inspector response is not valid JSON.") from exc
        except InspectorFailure:
            raise
        except (OSError, TimeoutError, struct.error) as exc:
            raise InspectorUnavailable("Quota inspector transport is unavailable.") from exc

    def _verify_socket_path(self, path: Path) -> None:
        try:
            if path != Path(os.path.normpath(path)) or not path.is_absolute():
                raise InspectorInvalid("Quota inspector socket path is not canonical.")
            current = Path("/")
            for component in path.parts[1:-1]:
                current /= component
                info = current.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or info.st_uid not in {0, self.inspector_uid}
                    or (
                        info.st_mode & 0o022
                        and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
                    )
                ):
                    raise InspectorInvalid(
                        "Quota inspector socket parent path is not protected.",
                    )
            socket_info = path.lstat()
            parent_info = path.parent.lstat()
            if (
                not stat.S_ISSOCK(socket_info.st_mode)
                or stat.S_ISLNK(socket_info.st_mode)
                or socket_info.st_uid != self.inspector_uid
                or socket_info.st_gid != self.inspector_gid
                or socket_info.st_mode & 0o007
                or not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != self.inspector_uid
                or parent_info.st_mode & 0o022
            ):
                raise InspectorInvalid("Quota inspector socket ownership or permissions are unsafe.")
        except InspectorFailure:
            raise
        except OSError as exc:
            raise InspectorUnavailable("Quota inspector socket is unavailable.") from exc

    def _verify_peer(self, connection: socket.socket) -> None:
        option = getattr(socket, "SO_PEERCRED", None)
        if option is None:
            raise InspectorUnavailable("Unix peer credential verification is unsupported.")
        try:
            pid, uid, gid = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i")),
            )
        except (OSError, struct.error) as exc:
            raise InspectorUnavailable("Quota inspector peer identity is unavailable.") from exc
        if pid <= 0 or uid != self.inspector_uid or gid != self.inspector_gid:
            raise InspectorInvalid("Quota inspector peer identity is unauthorized.")

    @staticmethod
    def _receive_exact(
        connection: socket.socket, size: int, deadline: float,
    ) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InspectorUnavailable("Quota inspector response exceeded its deadline.")
            connection.settimeout(remaining)
            chunk = connection.recv(size - len(chunks))
            if not chunk:
                raise InspectorUnavailable("Quota inspector closed an incomplete response.")
            chunks.extend(chunk)
        return bytes(chunks)

    def _parse_response(
        self, value: object, role_id: str, challenge: str,
    ) -> StorageObservation:
        if not isinstance(value, dict):
            raise InspectorInvalid("Quota inspector response must be an object.")
        status = value.get("status")
        expected_keys = _OBSERVATION_KEYS if status == "ok" else _ERROR_KEYS
        if set(value) != expected_keys:
            raise InspectorInvalid("Quota inspector response has an invalid schema.")
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != PROTOCOL_VERSION
            or value.get("message_type") != "storage_inspection_result"
            or value.get("role_id") != role_id
            or value.get("challenge") != challenge
            or not isinstance(value.get("helper_boot_epoch"), str)
            or not _EPOCH.fullmatch(value["helper_boot_epoch"])
            or value.get("inventory_generation") != self.inventory_generation
        ):
            raise InspectorInvalid("Quota inspector response binding is invalid.")
        if status == "unavailable":
            if not isinstance(value.get("error_code"), str) or not _ROLE.fullmatch(value["error_code"]):
                raise InspectorInvalid("Quota inspector error code is invalid.")
            raise InspectorUnavailable("Quota inspector reports the role unavailable.")
        if status == "inconsistent":
            if not isinstance(value.get("error_code"), str) or not _ROLE.fullmatch(value["error_code"]):
                raise InspectorInvalid("Quota inspector error code is invalid.")
            raise InspectorInconsistent("Quota inspector reports inconsistent storage state.")
        if status != "ok":
            raise InspectorInvalid("Quota inspector response status is invalid.")

        observation = self._validated_observation(value, role_id)
        now = int(time.time())
        if observation.observed_at > now + 2 or now - observation.observed_at > self.max_age_seconds:
            raise InspectorStale("Quota inspector response is outside its freshness window.")
        with self._epoch_lock:
            if self._boot_epoch is None:
                self._boot_epoch = observation.helper_boot_epoch
            elif observation.helper_boot_epoch != self._boot_epoch:
                raise InspectorStale("Quota inspector boot epoch changed during this Broker session.")
        return observation

    @staticmethod
    def _validated_observation(
        value: dict[str, object], role_id: str,
    ) -> StorageObservation:
        integer_fields = (
            "observed_at", "mount_id", "project_id", "byte_limit",
            "inode_limit", "bytes_used", "inodes_used", "filesystem_bytes",
            "filesystem_bytes_available", "filesystem_inodes",
            "filesystem_inodes_available", "owner_uid", "owner_gid",
        )
        if any(type(value.get(field)) is not int for field in integer_fields):
            raise InspectorInvalid("Quota inspector numeric fields must be integers.")
        if any(value[field] < 0 for field in integer_fields) or value["mount_id"] == 0:
            raise InspectorInvalid("Quota inspector numeric fields are outside their valid range.")
        for field in ("quota_configuration_enabled", "inheritance_enabled"):
            if type(value.get(field)) is not bool:
                raise InspectorInvalid("Quota inspector flags must be booleans.")
        filesystem_uuid = value.get("filesystem_uuid")
        mount_target = value.get("mount_target")
        try:
            normalized_uuid = str(uuid.UUID(filesystem_uuid)) if isinstance(filesystem_uuid, str) else ""
        except ValueError as exc:
            raise InspectorInvalid("Quota inspector filesystem UUID is invalid.") from exc
        if (
            not isinstance(filesystem_uuid, str)
            or normalized_uuid != filesystem_uuid.lower()
            or not isinstance(mount_target, str)
            or not mount_target.startswith("/")
            or mount_target != os.path.normpath(mount_target)
            or value.get("filesystem_type") != "ext4"
            or value.get("quota_mode") != "project"
        ):
            raise InspectorInvalid("Quota inspector filesystem identity is invalid.")
        if (
            value["byte_limit"] <= 0 or value["inode_limit"] <= 0
            or value["bytes_used"] > value["byte_limit"]
            or value["inodes_used"] > value["inode_limit"]
            or value["filesystem_bytes_available"] > value["filesystem_bytes"]
            or value["filesystem_inodes_available"] > value["filesystem_inodes"]
        ):
            raise InspectorInconsistent("Quota inspector returned inconsistent limits or usage.")
        return StorageObservation(
            role_id=role_id,
            helper_boot_epoch=value["helper_boot_epoch"],
            inventory_generation=value["inventory_generation"],
            observed_at=value["observed_at"],
            filesystem_uuid=filesystem_uuid,
            mount_target=mount_target,
            mount_id=value["mount_id"],
            filesystem_type=value["filesystem_type"],
            quota_mode=value["quota_mode"],
            quota_configuration_enabled=value["quota_configuration_enabled"],
            project_id=value["project_id"],
            inheritance_enabled=value["inheritance_enabled"],
            byte_limit=value["byte_limit"],
            inode_limit=value["inode_limit"],
            bytes_used=value["bytes_used"],
            inodes_used=value["inodes_used"],
            filesystem_bytes=value["filesystem_bytes"],
            filesystem_bytes_available=value["filesystem_bytes_available"],
            filesystem_inodes=value["filesystem_inodes"],
            filesystem_inodes_available=value["filesystem_inodes_available"],
            owner_uid=value["owner_uid"],
            owner_gid=value["owner_gid"],
        )


def inventory_generation(roles: tuple[StorageRole, ...]) -> str:
    payload = [
        {
            "role_id": role.role_id,
            "kind": role.kind,
            "path": str(role.path),
            "mount_path": str(role.mount_path),
            "filesystem_uuid": role.filesystem_uuid,
            "project_id": role.project_id,
            "byte_limit": role.byte_limit,
            "inode_limit": role.inode_limit,
            "owner_uid": role.owner_uid,
            "owner_gid": role.owner_gid,
        }
        for role in sorted(roles, key=lambda item: item.role_id)
    ]
    import hashlib
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")
