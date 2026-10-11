from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path


_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_UUID = re.compile(r"[a-fA-F0-9-]{36}\Z")
_STORAGE_ROLE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_STORAGE_KINDS = frozenset({
    "task_slot", "podman_graph", "podman_runtime", "image_scratch",
    "broker_state", "broker_logs", "temporary",
})
_STORAGE_CAPS = {
    "task_slot": (256 * 1024 * 1024, 4096),
    "podman_graph": (4 * 1024 * 1024 * 1024, 131_072),
    "podman_runtime": (512 * 1024 * 1024, 16_384),
    "image_scratch": (512 * 1024 * 1024, 16_384),
    "broker_state": (512 * 1024 * 1024, 16_384),
    "broker_logs": (512 * 1024 * 1024, 8192),
    "temporary": (512 * 1024 * 1024, 16_384),
}


@dataclass(frozen=True)
class QuotaSlot:
    slot_id: str
    path: Path
    mount_path: Path
    filesystem_uuid: str
    project_id: int
    byte_limit: int = 256 * 1024 * 1024
    inode_limit: int = 4096

    @classmethod
    def from_dict(cls, value: object) -> QuotaSlot:
        if not isinstance(value, dict) or set(value) - {
            "slot_id", "path", "mount_path", "filesystem_uuid", "project_id",
            "byte_limit", "inode_limit",
        }:
            raise ValueError("Quota slot configuration has an invalid shape")
        try:
            return cls(
                slot_id=value["slot_id"],
                path=Path(value["path"]),
                mount_path=Path(value["mount_path"]),
                filesystem_uuid=value["filesystem_uuid"],
                project_id=value["project_id"],
                byte_limit=value.get("byte_limit", 256 * 1024 * 1024),
                inode_limit=value.get("inode_limit", 4096),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("Quota slot configuration is incomplete") from exc

    def validate(self) -> None:
        if (
            not isinstance(self.slot_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", self.slot_id)
            or not self.path.is_absolute()
            or self.path != Path(os.path.normpath(self.path))
            or not self.mount_path.is_absolute()
            or self.mount_path != Path(os.path.normpath(self.mount_path))
            or not self.path.is_relative_to(self.mount_path)
            or self.path == self.mount_path
            or self.mount_path == Path("/")
            or self.path.name != self.slot_id
            or self.path.parent != self.mount_path / "tasks"
            or not isinstance(self.filesystem_uuid, str)
            or not _UUID.fullmatch(self.filesystem_uuid)
            or str(uuid.UUID(self.filesystem_uuid)) != self.filesystem_uuid.lower()
            or type(self.project_id) is not int
            or not 1 <= self.project_id <= 0xFFFFFFFF
            or type(self.byte_limit) is not int
            or not 16 * 1024 * 1024 <= self.byte_limit <= 256 * 1024 * 1024
            or type(self.inode_limit) is not int
            or not 128 <= self.inode_limit <= 4096
        ):
            raise ValueError("Quota slot configuration exceeds the fixed safety policy")


@dataclass(frozen=True)
class StorageRole:
    role_id: str
    kind: str
    path: Path
    mount_path: Path
    filesystem_uuid: str
    project_id: int
    byte_limit: int
    inode_limit: int
    owner_uid: int
    owner_gid: int

    @classmethod
    def from_dict(cls, value: object) -> StorageRole:
        if not isinstance(value, dict) or set(value) != {
            "role_id", "kind", "path", "mount_path", "filesystem_uuid",
            "project_id", "byte_limit", "inode_limit", "owner_uid", "owner_gid",
        }:
            raise ValueError("Storage role configuration has an invalid shape")
        try:
            return cls(
                role_id=value["role_id"],
                kind=value["kind"],
                path=Path(value["path"]),
                mount_path=Path(value["mount_path"]),
                filesystem_uuid=value["filesystem_uuid"],
                project_id=value["project_id"],
                byte_limit=value["byte_limit"],
                inode_limit=value["inode_limit"],
                owner_uid=value["owner_uid"],
                owner_gid=value["owner_gid"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("Storage role configuration is incomplete") from exc

    def validate(self) -> None:
        maximum_bytes, maximum_inodes = _STORAGE_CAPS.get(self.kind, (0, 0))
        if (
            not isinstance(self.role_id, str)
            or not _STORAGE_ROLE_ID.fullmatch(self.role_id)
            or self.kind not in _STORAGE_KINDS
            or not self.path.is_absolute()
            or self.path != Path(os.path.normpath(self.path))
            or not self.mount_path.is_absolute()
            or self.mount_path != Path(os.path.normpath(self.mount_path))
            or not self.path.is_relative_to(self.mount_path)
            or self.path == self.mount_path
            or self.mount_path == Path("/")
            or not isinstance(self.filesystem_uuid, str)
            or not _UUID.fullmatch(self.filesystem_uuid)
            or str(uuid.UUID(self.filesystem_uuid)) != self.filesystem_uuid.lower()
            or type(self.project_id) is not int
            or not 1 <= self.project_id <= 0xFFFFFFFF
            or type(self.byte_limit) is not int
            or not 1024 <= self.byte_limit <= maximum_bytes
            or type(self.inode_limit) is not int
            or not 16 <= self.inode_limit <= maximum_inodes
            or type(self.owner_uid) is not int or self.owner_uid < 0
            or type(self.owner_gid) is not int or self.owner_gid < 0
        ):
            raise ValueError("Storage role exceeds its fixed safety policy")


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    cpus: float
    memory_bytes: int
    pids: int
    workspace_bytes: int
    operation_seconds: int
    output_bytes: int


DEFAULT_PROFILE = ResourceProfile(
    name="python-small",
    cpus=1.0,
    memory_bytes=1024 * 1024 * 1024,
    pids=128,
    workspace_bytes=128 * 1024 * 1024,
    operation_seconds=300,
    output_bytes=1024 * 1024,
)


@dataclass(frozen=True)
class BrokerReadiness:
    ready: bool
    execution_available: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BrokerConfig:
    state_dir: Path
    podman: str
    image: str
    image_digest: str
    core_uid: int
    service_uid: int
    claim_key: bytes
    core_ca_file: Path
    broker_cert_file: Path
    broker_key_file: Path
    service_gid: int = field(default_factory=os.getegid)
    listen_port: int = 8766
    core_port: int = 8767
    profile: ResourceProfile = DEFAULT_PROFILE
    quota_slots: tuple[QuotaSlot, ...] = ()
    storage_roles: tuple[StorageRole, ...] = ()
    inspector_socket: Path | None = None
    inspector_uid: int = 0
    inspector_gid: int = 0
    inspector_timeout_seconds: float = 0.5
    inspector_max_age_seconds: int = 5
    storage_watchdog_interval_seconds: float = 1.0
    quota_volume_bytes: int = 16 * 1024 * 1024 * 1024
    quota_volume_inodes: int = 262_144
    quota_reserve_bytes: int = 8 * 1024 * 1024 * 1024
    quota_reserve_inodes: int = 16_384
    max_active_tasks: int = 1
    allowed_tools: frozenset[str] = frozenset({
        "read_file", "list_files", "write_file", "patch_file", "delete_file",
        "command", "verify", "collect",
    })

    def validate(self) -> None:
        if self.service_uid == 0 or self.service_uid == self.core_uid:
            raise ValueError("Broker must run as a distinct, unprivileged service identity")
        if (
            type(self.service_gid) is not int or self.service_gid < 1
            or self.service_uid != os.geteuid() or self.service_gid != os.getegid()
        ):
            raise ValueError("Broker configuration service identity does not match the current process")
        if not self.state_dir.is_absolute() or "," in str(self.state_dir):
            raise ValueError("Broker state path must be absolute and compatible with the fixed mount grammar")
        if len(self.claim_key) != 32:
            raise ValueError("Broker Core Ed25519 public key must contain exactly 32 bytes")
        for name, path in (
            ("Core CA", self.core_ca_file), ("Broker certificate", self.broker_cert_file),
            ("Broker private key", self.broker_key_file),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(f"{name} path must be absolute")
        if (
            type(self.listen_port) is not int or not 1 <= self.listen_port <= 65535
            or type(self.core_port) is not int or not 1 <= self.core_port <= 65535
        ):
            raise ValueError("Broker/Core private listener port is invalid")
        if len(self.quota_slots) > 4:
            raise ValueError("At most four pre-provisioned Broker quota slots are supported")
        if len({slot.slot_id for slot in self.quota_slots}) != len(self.quota_slots):
            raise ValueError("Quota slot identifiers must be unique")
        if len({slot.path for slot in self.quota_slots}) != len(self.quota_slots):
            raise ValueError("Quota slot paths must be unique")
        if len({slot.project_id for slot in self.quota_slots}) != len(self.quota_slots):
            raise ValueError("Quota project identifiers must be unique")
        for slot in self.quota_slots:
            slot.validate()
        if len({role.role_id for role in self.storage_roles}) != len(self.storage_roles):
            raise ValueError("Storage role identifiers must be unique")
        if len({role.path for role in self.storage_roles}) != len(self.storage_roles):
            raise ValueError("Storage role paths must be unique")
        if len({
            (role.filesystem_uuid, role.project_id) for role in self.storage_roles
        }) != len(self.storage_roles):
            raise ValueError("Storage roles must use distinct project-quota domains")
        for role in self.storage_roles:
            role.validate()
        if self.storage_roles:
            kinds = {role.kind for role in self.storage_roles}
            if kinds != _STORAGE_KINDS:
                raise ValueError("The storage inventory must cover every required Broker storage role")
            if any(
                sum(role.kind == kind for role in self.storage_roles) != 1
                for kind in _STORAGE_KINDS - {"task_slot"}
            ):
                raise ValueError("Each non-task storage role must have exactly one configured identity")
            if any(
                role.filesystem_uuid != self.storage_roles[0].filesystem_uuid
                or role.mount_path != self.storage_roles[0].mount_path
                for role in self.storage_roles
            ):
                raise ValueError("Storage roles must share the configured bounded filesystem")
            state_roles = [role for role in self.storage_roles if role.kind == "broker_state"]
            if state_roles[0].path != self.state_dir:
                raise ValueError("Broker state path must use its inventoried bounded storage role")
            if any(
                role.owner_uid != self.service_uid or role.owner_gid != self.service_gid
                for role in self.storage_roles
            ):
                raise ValueError("Broker storage roles must be owned by the dedicated service identity")
            slot_roles = {
                role.role_id: role for role in self.storage_roles if role.kind == "task_slot"
            }
            if len(slot_roles) != len(self.quota_slots):
                raise ValueError("Every quota slot must have exactly one storage role")
            for slot in self.quota_slots:
                role = slot_roles.get(f"task-{slot.slot_id}")
                if role is None or (
                    role.path != slot.path
                    or role.mount_path != slot.mount_path
                    or role.filesystem_uuid != slot.filesystem_uuid
                    or role.project_id != slot.project_id
                    or role.byte_limit != slot.byte_limit
                    or role.inode_limit != slot.inode_limit
                ):
                    raise ValueError("Task storage role differs from its fixed quota-slot inventory")
        if self.inspector_socket is not None and (
            not self.inspector_socket.is_absolute()
            or self.inspector_socket != Path(os.path.normpath(self.inspector_socket))
        ):
            raise ValueError("Inspector socket path must be absolute and canonical")
        if (
            type(self.inspector_uid) is not int or self.inspector_uid < 0
            or type(self.inspector_gid) is not int or self.inspector_gid < 0
            or type(self.inspector_timeout_seconds) not in {int, float}
            or not 0.05 <= self.inspector_timeout_seconds <= 2
            or type(self.inspector_max_age_seconds) is not int
            or not 1 <= self.inspector_max_age_seconds <= 30
            or type(self.storage_watchdog_interval_seconds) not in {int, float}
            or not 0.1 <= self.storage_watchdog_interval_seconds < self.inspector_max_age_seconds
        ):
            raise ValueError("Inspector or storage-watchdog timing policy is invalid")
        role_bytes = sum(role.byte_limit for role in self.storage_roles)
        role_inodes = sum(role.inode_limit for role in self.storage_roles)
        if self.storage_roles and (
            role_bytes + self.quota_reserve_bytes > self.quota_volume_bytes
            or role_inodes + self.quota_reserve_inodes > self.quota_volume_inodes
        ):
            raise ValueError("Storage-role entitlements exceed the configured aggregate policy")
        if self.quota_slots and any(
            self.profile.workspace_bytes > slot.byte_limit
            for slot in self.quota_slots
        ):
            raise ValueError("Workspace resource profile exceeds a configured task-slot quota")
        if (
            type(self.quota_volume_bytes) is not int
            or not 16 * 1024 * 1024 * 1024 <= self.quota_volume_bytes <= 1024 * 1024 * 1024 * 1024
            or type(self.quota_volume_inodes) is not int
            or not 204_800 <= self.quota_volume_inodes <= 4_194_304
            or type(self.quota_reserve_bytes) is not int
            or self.quota_reserve_bytes < 0
            or type(self.quota_reserve_inodes) is not int
            or self.quota_reserve_inodes < 0
            or type(self.max_active_tasks) is not int
            or not 1 <= self.max_active_tasks <= 4
        ):
            raise ValueError("Broker aggregate quota or active-task policy is invalid")
        if not _DIGEST.fullmatch(self.image_digest):
            raise ValueError("Broker image digest must be a pinned sha256 digest")
        if (
            not isinstance(self.image, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]{0,255}@sha256:[a-f0-9]{64}", self.image)
            or not self.image.endswith(f"@{self.image_digest}")
        ):
            raise ValueError("Broker image must be the configured repository pinned to its digest")
        if not _PROFILE.fullmatch(self.profile.name):
            raise ValueError("Broker resource profile name is invalid")
        if (
            not 0 < self.profile.cpus <= 8
            or not 64 * 1024 * 1024 <= self.profile.memory_bytes <= 8 * 1024 * 1024 * 1024
            or not 16 <= self.profile.pids <= 1024
            or not 16 * 1024 * 1024 <= self.profile.workspace_bytes <= 2 * 1024 * 1024 * 1024
            or not 1 <= self.profile.operation_seconds <= 1800
            or not 1024 <= self.profile.output_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("Broker resource profile exceeds fixed safety ceilings")
        if self.allowed_tools - {
            "read_file", "list_files", "write_file", "patch_file", "delete_file",
            "command", "verify", "collect",
        }:
            raise ValueError("Broker tool allowlist contains an unsupported operation")
    @classmethod
    def from_env(cls) -> BrokerConfig:
        required = (
            "SYNAI_BROKER_CORE_UID", "SYNAI_BROKER_IMAGE",
            "SYNAI_BROKER_IMAGE_DIGEST", "SYNAI_BROKER_CLAIM_PUBLIC_KEY",
            "SYNAI_BROKER_STATE_DIR", "SYNAI_BROKER_CORE_CA",
            "SYNAI_BROKER_CERT", "SYNAI_BROKER_KEY",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError("Broker configuration is incomplete: " + ", ".join(missing))
        image_digest = os.environ["SYNAI_BROKER_IMAGE_DIGEST"]
        config = cls(
            state_dir=Path(os.environ["SYNAI_BROKER_STATE_DIR"]),
            podman=os.environ.get("SYNAI_BROKER_PODMAN", "podman"),
            image=os.environ["SYNAI_BROKER_IMAGE"],
            image_digest=image_digest,
            core_uid=int(os.environ["SYNAI_BROKER_CORE_UID"]),
            service_uid=os.geteuid(),
            service_gid=int(os.environ.get("SYNAI_BROKER_SERVICE_GID", str(os.getegid()))),
            claim_key=_decode_key(os.environ["SYNAI_BROKER_CLAIM_PUBLIC_KEY"]),
            core_ca_file=Path(os.environ["SYNAI_BROKER_CORE_CA"]),
            broker_cert_file=Path(os.environ["SYNAI_BROKER_CERT"]),
            broker_key_file=Path(os.environ["SYNAI_BROKER_KEY"]),
            listen_port=int(os.environ.get("SYNAI_BROKER_PORT", "8766")),
            core_port=int(os.environ.get("SYNAI_CORE_BROKER_PORT", "8767")),
            quota_slots=_quota_slots_from_env(),
            storage_roles=_storage_roles_from_env(),
            inspector_socket=(
                Path(os.environ["SYNAI_BROKER_INSPECTOR_SOCKET"])
                if os.environ.get("SYNAI_BROKER_INSPECTOR_SOCKET") else None
            ),
            inspector_uid=int(os.environ.get("SYNAI_BROKER_INSPECTOR_UID", "0")),
            inspector_gid=int(os.environ.get("SYNAI_BROKER_INSPECTOR_GID", "0")),
            storage_watchdog_interval_seconds=float(os.environ.get(
                "SYNAI_BROKER_STORAGE_WATCHDOG_INTERVAL_SECONDS", "1.0",
            )),
            quota_volume_bytes=int(os.environ.get(
                "SYNAI_BROKER_QUOTA_VOLUME_BYTES", str(16 * 1024 * 1024 * 1024),
            )),
            quota_volume_inodes=int(os.environ.get(
                "SYNAI_BROKER_QUOTA_VOLUME_INODES", "262144",
            )),
            quota_reserve_bytes=int(os.environ.get(
                "SYNAI_BROKER_QUOTA_RESERVE_BYTES", str(8 * 1024 * 1024 * 1024),
            )),
            quota_reserve_inodes=int(os.environ.get(
                "SYNAI_BROKER_QUOTA_RESERVE_INODES", "16384",
            )),
            max_active_tasks=int(os.environ.get("SYNAI_BROKER_MAX_ACTIVE_TASKS", "1")),
        )
        config.validate()
        return config

    def readiness(self) -> BrokerReadiness:
        checks: dict[str, bool] = {
            "distinct_unprivileged_identity": self.service_uid != 0 and self.service_uid != self.core_uid,
            "pinned_image_reference": bool(_DIGEST.fullmatch(self.image_digest))
            and self.image.endswith(f"@{self.image_digest}"),
            "state_directory_private": _private_state_directory(self.state_dir),
            "podman_available": shutil.which(self.podman) is not None,
            "mtls_material": all(
                _private_file(path, private=private)
                for path, private in (
                    (self.core_ca_file, False),
                    (self.broker_cert_file, False),
                    (self.broker_key_file, True),
                )
            ),
            "rootless_podman": False,
            "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
            "seccomp_profile": Path("/usr/share/containers/seccomp.json").is_file(),
            "linux_security_module": Path("/sys/kernel/security/lsm").exists()
            and _active_lsm(),
            "workspace_quota": False,
            "storage_inventory": bool(self.storage_roles),
            "storage_inspector": self.inspector_socket is not None,
            "approved_image_present": False,
        }
        reasons = tuple(name for name, passed in checks.items() if not passed)
        # Storage limits and an active LSM are mandatory. Neither is safely
        # inferred from CLI support or from a configured numeric ceiling.
        return BrokerReadiness(not reasons, not reasons, checks, reasons)


def _quota_slots_from_env() -> tuple[QuotaSlot, ...]:
    encoded = os.environ.get("SYNAI_BROKER_QUOTA_SLOTS")
    if not encoded:
        return ()
    try:
        value = json.loads(encoded)
        if not isinstance(value, list):
            raise ValueError
        return tuple(QuotaSlot.from_dict(entry) for entry in value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("SYNAI_BROKER_QUOTA_SLOTS must be a valid JSON array") from exc


def _storage_roles_from_env() -> tuple[StorageRole, ...]:
    encoded = os.environ.get("SYNAI_BROKER_STORAGE_ROLES")
    if not encoded:
        return ()
    try:
        value = json.loads(encoded)
        if not isinstance(value, list):
            raise ValueError
        return tuple(StorageRole.from_dict(entry) for entry in value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("SYNAI_BROKER_STORAGE_ROLES must be a valid JSON array") from exc


def _decode_key(value: str) -> bytes:
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("Broker keys must be hexadecimal") from exc
    if len(result) < 32:
        raise ValueError("Broker keys must contain at least 32 bytes")
    return result


def _private_state_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        return path.is_dir() and not path.is_symlink() and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0
    except OSError:
        return False


def _active_lsm() -> bool:
    try:
        return any(item in {"apparmor", "selinux"} for item in
                   Path("/sys/kernel/security/lsm").read_text().strip().split(","))
    except OSError:
        return False


def _private_file(path: Path, *, private: bool) -> bool:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size <= 0:
            return False
        return not private or (
            info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0
        )
    except OSError:
        return False
