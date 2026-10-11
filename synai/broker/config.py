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
    quota_volume_bytes: int = 16 * 1024 * 1024 * 1024
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
        if any(slot.mount_path != self.quota_slots[0].mount_path for slot in self.quota_slots):
            raise ValueError("All Broker quota slots must use one dedicated filesystem")
        for slot in self.quota_slots:
            slot.validate()
        if self.quota_slots and any(
            self.profile.workspace_bytes > slot.byte_limit
            for slot in self.quota_slots
        ):
            raise ValueError("Workspace resource profile exceeds a configured task-slot quota")
        if (
            type(self.quota_volume_bytes) is not int
            or not 16 * 1024 * 1024 * 1024 <= self.quota_volume_bytes <= 1024 * 1024 * 1024 * 1024
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
            quota_volume_bytes=int(os.environ.get(
                "SYNAI_BROKER_QUOTA_VOLUME_BYTES", str(16 * 1024 * 1024 * 1024),
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

    def readiness(self, *, workspace_quota_verified: bool = False) -> BrokerReadiness:
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
            "workspace_quota": workspace_quota_verified,
            "podman_storage_separate": False,
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
