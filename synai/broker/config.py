from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


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
    service_key: bytes
    claim_key: bytes
    socket_gid: int
    profile: ResourceProfile = DEFAULT_PROFILE
    socket_path: Path | None = None
    allowed_tools: frozenset[str] = frozenset({
        "read_file", "list_files", "write_file", "patch_file", "delete_file",
        "command", "verify", "collect",
    })

    def validate(self) -> None:
        if self.service_uid == 0 or self.service_uid == self.core_uid:
            raise ValueError("Broker must run as a distinct, unprivileged service identity")
        if self.service_uid != os.geteuid():
            raise ValueError("Broker configuration service identity does not match the current process")
        if not self.state_dir.is_absolute() or "," in str(self.state_dir):
            raise ValueError("Broker state path must be absolute and compatible with the fixed mount grammar")
        if len(self.service_key) < 32 or len(self.claim_key) < 32:
            raise ValueError("Broker service key and Core Ed25519 public key must each contain at least 32 bytes")
        if len(self.claim_key) != 32:
            raise ValueError("Broker Core Ed25519 public key must contain exactly 32 bytes")
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
        if self.socket_path is not None and not self.socket_path.is_absolute():
            raise ValueError("Broker socket path must be absolute")
        if type(self.socket_gid) is not int or self.socket_gid < 0:
            raise ValueError("Broker socket group ID must be a non-negative integer")

    @classmethod
    def from_env(cls) -> BrokerConfig:
        required = (
            "SYNAI_BROKER_CORE_UID", "SYNAI_BROKER_IMAGE",
            "SYNAI_BROKER_IMAGE_DIGEST", "SYNAI_BROKER_SERVICE_KEY",
            "SYNAI_BROKER_CLAIM_PUBLIC_KEY", "SYNAI_BROKER_STATE_DIR",
            "SYNAI_BROKER_SOCKET_GID",
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
            service_key=_decode_key(os.environ["SYNAI_BROKER_SERVICE_KEY"]),
            claim_key=_decode_key(os.environ["SYNAI_BROKER_CLAIM_PUBLIC_KEY"]),
            socket_gid=int(os.environ["SYNAI_BROKER_SOCKET_GID"]),
            socket_path=Path(os.environ["SYNAI_BROKER_SOCKET"])
            if os.environ.get("SYNAI_BROKER_SOCKET") else None,
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
            "rootless_podman": False,
            "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
            "seccomp_profile": Path("/usr/share/containers/seccomp.json").is_file(),
            "linux_security_module": Path("/sys/kernel/security/lsm").exists()
            and _active_lsm(),
            "workspace_quota": False,
            "approved_image_present": False,
        }
        reasons = tuple(name for name, passed in checks.items() if not passed)
        # Storage limits and an active LSM are mandatory. Neither is safely
        # inferred from CLI support or from a configured numeric ceiling.
        return BrokerReadiness(not reasons, not reasons, checks, reasons)


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
