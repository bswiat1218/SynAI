from __future__ import annotations

import ctypes
import fcntl
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from synai.broker.config import BrokerConfig, QuotaSlot
from synai.broker.protocol import BrokerError


_EXT4_SUPER_MAGIC = 0xEF53
_FS_IOC_FSGETXATTR = 0x801C581F
_FS_XFLAG_PROJINHERIT = 0x00000200
_Q_GETQUOTA = 0x80000702
_PRJQUOTA = 2
_QIF_BLIMITS = 1 << 0
_QIF_SPACE = 1 << 1
_QIF_ILIMITS = 1 << 2
_QIF_INODES = 1 << 3
_QIF_REQUIRED = _QIF_BLIMITS | _QIF_SPACE | _QIF_ILIMITS | _QIF_INODES
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class QuotaUsage:
    byte_limit: int
    inode_limit: int
    bytes_used: int
    inodes_used: int


class QuotaInspector(Protocol):
    def inspect(self, slot: QuotaSlot, *, uid: int, gid: int) -> QuotaUsage: ...


class LinuxQuotaInspector:
    """Read-only inspection of the live mount, project ID and project quota."""

    def inspect(self, slot: QuotaSlot, *, uid: int, gid: int) -> QuotaUsage:
        descriptors: list[int] = []
        try:
            mount_fd, mount_chain = _open_directory_chain(slot.mount_path)
            descriptors.extend(mount_chain)
            slot_fd, slot_chain = _open_directory_chain(slot.path)
            descriptors.extend(slot_chain)
            mount = _mount_for_fd(mount_fd)
            slot_mount = _mount_for_fd(slot_fd)
            if (
                mount.mount_id != slot_mount.mount_id
                or mount.target != str(slot.mount_path)
                or mount.root != "/"
                or mount.filesystem != "ext4"
                or not ({"prjquota", "pquota"} & (mount.mount_options | mount.super_options))
            ):
                raise ValueError("Quota slot is not on the expected dedicated ext4 project-quota mount")
            _verify_filesystem_device(slot.filesystem_uuid, mount.device)
            _verify_directory_ownership(mount_fd, expected_uid=0, expected_gid=None, writable=False)
            parent_fd = slot_chain[-2]
            _verify_directory_ownership(parent_fd, expected_uid=0, expected_gid=None, writable=False)
            _verify_directory_ownership(slot_fd, expected_uid=uid, expected_gid=gid, writable=True)
            if _mount_for_fd(parent_fd).mount_id != mount.mount_id:
                raise ValueError("Quota slot parent crosses a mount boundary")
            project_id, flags = _project_attributes(slot_fd)
            if project_id != slot.project_id or not flags & _FS_XFLAG_PROJINHERIT:
                raise ValueError("Quota slot project ID or inheritance flag differs from its inventory")
            return _read_project_quota(slot.mount_path, slot.project_id)
        except BrokerError:
            raise
        except (OSError, ValueError, IndexError, struct.error) as exc:
            raise BrokerError(
                "workspace_quota_unverified",
                "Broker cannot independently verify the configured project quota slot.",
                503,
            ) from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


@dataclass(frozen=True)
class _Mount:
    mount_id: int
    device: str
    root: str
    target: str
    mount_options: frozenset[str]
    filesystem: str
    super_options: frozenset[str]


class QuotaSlotManager:
    """Fail-closed quota inventory and path resolver for Broker-owned slots."""

    def __init__(
        self,
        config: BrokerConfig,
        registry: object,
        inspector: QuotaInspector | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.inspector = inspector or LinuxQuotaInspector()
        self.verified = False
        self.verified_slot_ids: frozenset[str] = frozenset()
        self.reasons: tuple[str, ...] = ("workspace_quota_unverified",)

    def verify_startup(self) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if not self.config.quota_slots:
            reasons.append("quota_slot_inventory_missing")
        if not _process_identity_is_restricted(
            self.config.service_uid, self.config.service_gid,
        ):
            reasons.append("quota_bypass_privilege")
        if hasattr(self.registry, "slot_inventory_matches"):
            for slot in self.config.quota_slots:
                if not self.registry.slot_inventory_matches(slot):
                    reasons.append("quota_slot_inventory_mismatch")
                    break
        else:
            reasons.append("quota_slot_registry_unavailable")
        usage: list[tuple[QuotaSlot, QuotaUsage]] = []
        physical_reasons: list[str] = []
        verified_ids: set[str] = set()
        slot_records = {
            row["slot_id"]: row for row in self.registry.slot_configurations()
        } if hasattr(self.registry, "slot_configurations") else {}
        for slot in self.config.quota_slots:
            try:
                current = self.inspector.inspect(
                    slot, uid=self.config.service_uid, gid=self.config.service_gid,
                )
                self._verify_slot_contents(slot, slot_records.get(slot.slot_id))
                if os.stat(self.config.state_dir).st_dev == os.stat(slot.path).st_dev:
                    raise ValueError("Broker registry/state shares the task quota filesystem")
                if (
                    current.byte_limit != slot.byte_limit
                    or current.inode_limit != slot.inode_limit
                    or current.bytes_used > current.byte_limit
                    or current.inodes_used > current.inode_limit
                ):
                    raise ValueError("Live project quota does not match its configured hard limits")
                usage.append((slot, current))
                verified_ids.add(slot.slot_id)
            except (BrokerError, OSError, ValueError):
                physical_reasons.append(f"quota_slot_unverified:{slot.slot_id}")
        if physical_reasons:
            reasons.extend(physical_reasons)
        if len(usage) == len(self.config.quota_slots) and usage:
            try:
                values = os.statvfs(self.config.quota_slots[0].mount_path)
                available_bytes = values.f_bavail * values.f_frsize
                available_inodes = values.f_favail
                missing_bytes = sum(
                    slot.byte_limit - live.bytes_used for slot, live in usage
                )
                missing_inodes = sum(
                    slot.inode_limit - live.inodes_used for slot, live in usage
                )
                if (
                    values.f_blocks * values.f_frsize < self.config.quota_volume_bytes
                    or available_bytes < missing_bytes + self.config.quota_reserve_bytes
                    or available_inodes < missing_inodes + self.config.quota_reserve_inodes
                ):
                    reasons.append("quota_aggregate_headroom")
            except OSError:
                reasons.append("quota_aggregate_headroom_unverified")
            try:
                task_root = self.config.quota_slots[0].path.parent
                task_fd, descriptors = _open_directory_chain(task_root)
                try:
                    actual = set(os.listdir(task_fd))
                    expected = {slot.slot_id for slot in self.config.quota_slots}
                    if actual != expected:
                        reasons.append("quota_slot_directory_inventory_mismatch")
                finally:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)
            except OSError:
                reasons.append("quota_slot_directory_inventory_unverified")
        if hasattr(self.registry, "unassigned_legacy_tasks") and self.registry.unassigned_legacy_tasks():
            reasons.append("legacy_task_slot_assignment_missing")
        if hasattr(self.registry, "slot_configurations"):
            configured = {slot.slot_id for slot in self.config.quota_slots}
            recorded = {row["slot_id"] for row in self.registry.slot_configurations()}
            if configured != recorded:
                reasons.append("quota_slot_inventory_mismatch")
        self.reasons = tuple(dict.fromkeys(reasons))
        blocked_inventory = {
            "quota_slot_inventory_missing", "quota_bypass_privilege",
            "quota_slot_inventory_mismatch", "quota_slot_registry_unavailable",
            "quota_slot_directory_inventory_mismatch",
            "quota_slot_directory_inventory_unverified",
            "quota_aggregate_headroom", "quota_aggregate_headroom_unverified",
            "legacy_task_slot_assignment_missing",
        }
        self.verified = bool(verified_ids) and not any(
            reason in blocked_inventory or reason.startswith("quota_slot_unverified:")
            for reason in self.reasons
        )
        self.verified_slot_ids = frozenset(verified_ids if self.verified else ())
        return self.verified, self.reasons

    def _verify_slot_contents(self, slot: QuotaSlot, record: object | None) -> None:
        if record is None:
            raise ValueError("Quota slot has no durable inventory record")
        if any(record[name] != value for name, value in (
            ("path", str(slot.path)),
            ("filesystem_uuid", slot.filesystem_uuid),
            ("project_id", slot.project_id),
            ("byte_limit", slot.byte_limit),
            ("inode_limit", slot.inode_limit),
        )):
            raise ValueError("Quota slot identity differs from its durable inventory")
        task_id = record["task_id"]
        if record["state"] == "free":
            if task_id is not None or record["allocation_id"] is not None or record["owner_token_hash"] is not None:
                raise ValueError("Free quota slot retains task ownership")
        elif (
            record["state"] not in {"allocated", "release_pending", "quarantined"}
            or not task_id
            or not record["allocation_id"]
            or not record["owner_token_hash"]
        ):
            raise ValueError("Allocated quota slot has incomplete durable ownership")
        else:
            self.registry.task_slot(task_id, allow_quarantined=True)
        try:
            slot_fd, descriptors = _open_directory_chain(slot.path)
        except OSError as exc:
            self.registry.quarantine_slot(task_id)
            raise BrokerError(
                "workspace_path_unverified",
                "Assigned quota-slot path changed during task resolution.",
                409,
            ) from exc
        try:
            entries = set(os.listdir(slot_fd))
            if entries - ({task_id} if task_id else set()):
                raise ValueError("Quota slot contains unowned content")
            if task_id and entries:
                task_fd = os.open(
                    task_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=slot_fd,
                )
                try:
                    info = os.fstat(task_fd)
                    project_id, flags = _project_attributes(task_fd)
                    if (
                        info.st_uid != self.config.service_uid
                        or info.st_gid != self.config.service_gid
                        or stat.S_IMODE(info.st_mode) != 0o700
                        or project_id != slot.project_id
                        or not flags & _FS_XFLAG_PROJINHERIT
                    ):
                        raise ValueError("Quota task directory identity or inheritance changed")
                finally:
                    os.close(task_fd)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def resolve_task_root(self, task_id: str, *, allow_quarantined: bool = False) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise BrokerError("task_identity_invalid", "Task identity is invalid.", 422)
        if not self.verified:
            raise BrokerError(
                "workspace_quota_unverified",
                "Task workspace quota inventory is unavailable.",
                503,
            )
        assignment = self.registry.task_slot(task_id, allow_quarantined=allow_quarantined)
        slot = next((item for item in self.config.quota_slots if item.slot_id == assignment["slot_id"]), None)
        if (
            slot is None or assignment["path"] != str(slot.path)
            or slot.slot_id not in self.verified_slot_ids
        ):
            raise BrokerError(
                "workspace_slot_mismatch",
                "Durable task slot ownership differs from the operator inventory.",
                409,
            )
        try:
            live = self.inspector.inspect(
                slot, uid=self.config.service_uid, gid=self.config.service_gid,
            )
        except BrokerError:
            self.registry.quarantine_slot(task_id)
            raise
        if live.byte_limit != slot.byte_limit or live.inode_limit != slot.inode_limit:
            self.registry.quarantine_slot(task_id)
            raise BrokerError("workspace_slot_mismatch", "Live task quota differs from its assigned slot.", 409)
        try:
            slot_fd, descriptors = _open_directory_chain(slot.path)
        except OSError as exc:
            self.registry.quarantine_slot(task_id)
            raise BrokerError(
                "workspace_path_unverified",
                "Assigned quota-slot path changed before task creation.",
                409,
            ) from exc
        try:
            try:
                task_fd = os.open(
                    task_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=slot_fd,
                )
            except FileNotFoundError:
                return slot.path / task_id
            except OSError as exc:
                self.registry.quarantine_slot(task_id)
                raise BrokerError(
                    "workspace_path_unverified",
                    "Task workspace path cannot be opened without following redirection.",
                    409,
                ) from exc
            try:
                info = os.fstat(task_fd)
                project_id, flags = _project_attributes(task_fd)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != self.config.service_uid
                    or info.st_gid != self.config.service_gid
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or project_id != slot.project_id
                    or not flags & _FS_XFLAG_PROJINHERIT
                ):
                    self.registry.quarantine_slot(task_id)
                    raise BrokerError(
                        "workspace_slot_mismatch",
                        "Task directory identity or inherited quota differs from its assigned slot.",
                        409,
                    )
            except OSError as exc:
                self.registry.quarantine_slot(task_id)
                raise BrokerError(
                    "workspace_path_unverified",
                    "Task directory identity cannot be independently verified.",
                    409,
                ) from exc
            finally:
                os.close(task_fd)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return slot.path / task_id

    def create_task_root(self, task_id: str) -> Path:
        task_root = self.resolve_task_root(task_id)
        slot = next(
            item for item in self.config.quota_slots
            if task_root.parent == item.path
        )
        slot_fd, descriptors = _open_directory_chain(slot.path)
        task_fd: int | None = None
        try:
            os.mkdir(task_id, mode=0o700, dir_fd=slot_fd)
            task_fd = os.open(
                task_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=slot_fd,
            )
            info = os.fstat(task_fd)
            project_id, flags = _project_attributes(task_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.config.service_uid
                or info.st_gid != self.config.service_gid
                or stat.S_IMODE(info.st_mode) != 0o700
                or project_id != slot.project_id
                or not flags & _FS_XFLAG_PROJINHERIT
            ):
                raise ValueError("New task directory did not inherit its configured project quota")
            return task_root
        except (OSError, ValueError, BrokerError) as exc:
            self.registry.quarantine_slot(task_id)
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError(
                "workspace_project_inheritance_unverified",
                "Broker could not verify project-quota inheritance before staging.",
                503,
            ) from exc
        finally:
            if task_fd is not None:
                os.close(task_fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def verify_releasable(self, task_id: str) -> Path:
        task_root = self.resolve_task_root(task_id, allow_quarantined=True)
        assignment = self.registry.task_slot(task_id)
        try:
            info = task_root.lstat()
        except FileNotFoundError:
            return task_root
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != self.config.service_uid
            or info.st_gid != self.config.service_gid
            or task_root.parent != Path(assignment["path"])
        ):
            raise BrokerError(
                "workspace_cleanup_refused",
                "Task workspace ownership or slot identity cannot be verified.",
                409,
            )
        return task_root

    def recover_pending_releases(self) -> None:
        for slot_record in self.registry.pending_slot_releases():
            task_id = slot_record["task_id"]
            try:
                if slot_record["task_state"] != "removed" or not slot_record["release_evidence"]:
                    raise BrokerError("slot_release_refused", "Pending slot release has incomplete durable evidence.", 409)
                container = self.registry.container_record(task_id)
                if container is not None and (
                    container["lifecycle_state"] != "removed"
                    or not str(container["reconciliation_evidence"] or "").startswith("verified_absent:")
                ):
                    raise BrokerError(
                        "slot_release_refused",
                        "Container removal has not been independently reconciled.",
                        409,
                    )
                if self.registry.task_has_unresolved_operations(task_id):
                    raise BrokerError("slot_release_refused", "Task has uncertain operation provenance.", 409)
                task_root = self.resolve_task_root(task_id, allow_quarantined=True)
                try:
                    info = task_root.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != self.config.service_uid
                        or info.st_gid != self.config.service_gid
                        or any(task_root.iterdir())
                    ):
                        raise BrokerError("slot_release_refused", "Pending task directory is not demonstrably empty.", 409)
                    task_root.rmdir()
                self.registry.release_slot(
                    task_id,
                    str(slot_record["release_evidence"]),
                )
            except (BrokerError, OSError):
                self.registry.quarantine_slot(task_id)
        self.verify_startup()


def _process_identity_is_restricted(expected_uid: int, expected_gid: int) -> bool:
    if os.geteuid() != expected_uid or os.getegid() != expected_gid or expected_uid == 0:
        return False
    try:
        status = Path("/proc/self/status").read_text()
        cap_line = next(line for line in status.splitlines() if line.startswith("CapEff:"))
        capabilities = int(cap_line.split()[1], 16)
        if capabilities != 0:
            return False
        groups = os.getgroups()
        return set(groups) <= {expected_gid}
    except (OSError, StopIteration, ValueError):
        return False


def _open_directory_chain(path: Path) -> tuple[int, list[int]]:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise ValueError("Quota paths must be canonical absolute paths")
    descriptors: list[int] = []
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptors.append(current)
    try:
        for component in path.parts[1:]:
            current = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            descriptors.append(current)
        return current, descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _mount_for_fd(descriptor: int) -> _Mount:
    fs_info = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(descriptor, fs_info) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if struct.unpack_from("@l", fs_info.raw)[0] != _EXT4_SUPER_MAGIC:
        raise ValueError("Filesystem magic is not ext4")
    try:
        fdinfo = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
        mount_id = int(next(line.split(":", 1)[1] for line in fdinfo.splitlines() if line.startswith("mnt_id:")))
        rows = Path("/proc/self/mountinfo").read_text().splitlines()
    except (OSError, StopIteration, ValueError) as exc:
        raise ValueError("Kernel mount identity is unavailable") from exc
    for row in rows:
        fields = row.split()
        separator = fields.index("-")
        if int(fields[0]) != mount_id:
            continue
        return _Mount(
            mount_id=mount_id,
            device=fields[2],
            root=_unescape_mount(fields[3]),
            target=_unescape_mount(fields[4]),
            mount_options=frozenset(fields[5].split(",")),
            filesystem=fields[separator + 1],
            super_options=frozenset(fields[separator + 3].split(",")),
        )
    raise ValueError("Kernel mount identity does not resolve")


def _unescape_mount(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _verify_filesystem_device(filesystem_uuid: str, device: str) -> None:
    target = Path("/dev/disk/by-uuid") / filesystem_uuid
    info = target.stat()
    if not stat.S_ISBLK(info.st_mode) or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != device:
        raise ValueError("Expected filesystem UUID does not match the mounted block device")


def _verify_directory_ownership(
    descriptor: int, *, expected_uid: int, expected_gid: int | None, writable: bool,
) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid:
        raise ValueError("Quota directory ownership is unexpected")
    if expected_gid is not None and info.st_gid != expected_gid:
        raise ValueError("Quota directory group ownership is unexpected")
    if writable:
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Quota slot permissions are not private")
    elif info.st_mode & 0o022:
        raise ValueError("Quota mount parent can be modified by an untrusted identity")


def _project_attributes(descriptor: int) -> tuple[int, int]:
    buffer = bytearray(28)
    fcntl.ioctl(descriptor, _FS_IOC_FSGETXATTR, buffer, True)
    flags, _, _, project_id, _, _ = struct.unpack("=IIIII8s", buffer)
    return project_id, flags


def _read_project_quota(mount_path: Path, project_id: int) -> QuotaUsage:
    class Dqblk(ctypes.Structure):
        _fields_ = [
            ("bhard", ctypes.c_uint64), ("bsoft", ctypes.c_uint64),
            ("space", ctypes.c_uint64), ("ihard", ctypes.c_uint64),
            ("isoft", ctypes.c_uint64), ("inodes", ctypes.c_uint64),
            ("btime", ctypes.c_uint64), ("itime", ctypes.c_uint64),
            ("valid", ctypes.c_uint32),
        ]

    quota = Dqblk()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.quotactl.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    libc.quotactl.restype = ctypes.c_int
    result = libc.quotactl(
        _Q_GETQUOTA, os.fsencode(mount_path), project_id, ctypes.byref(quota),
    )
    if result != 0:
        code = ctypes.get_errno()
        raise BrokerError(
            "workspace_quota_unreadable",
            "Broker lacks an unprivileged read-only view of live project quota state.",
            503,
        ) from OSError(code, os.strerror(code))
    if quota.valid & _QIF_REQUIRED != _QIF_REQUIRED:
        raise ValueError("Kernel did not return all project quota limit and usage fields")
    return QuotaUsage(
        byte_limit=quota.bhard * 1024,
        inode_limit=quota.ihard,
        bytes_used=quota.space,
        inodes_used=quota.inodes,
    )
