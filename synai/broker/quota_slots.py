from __future__ import annotations

import ctypes
import fcntl
import os
import re
import stat
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from synai.broker.filesystem import Directory, mount_identity, open_beneath, remove_tree
from typing import Protocol

from synai.broker.config import BrokerConfig, QuotaSlot, StorageRole
from synai.broker.protocol import BrokerError
from synai.broker.storage_inspector import (
    InspectorFailure,
    StorageObservation,
    UnixStorageInspectorClient,
    inventory_generation,
)


_EXT4_SUPER_MAGIC = 0xEF53
_FS_IOC_FSGETXATTR = 0x801C581F
_FS_XFLAG_PROJINHERIT = 0x00000200
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class QuotaUsage:
    byte_limit: int
    inode_limit: int
    bytes_used: int
    inodes_used: int


class QuotaInspector(Protocol):
    def inspect(self, role_id: str) -> StorageObservation: ...


class LinuxQuotaInspector:
    """Broker-side local socket client; never performs privileged quota syscalls."""

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.client = UnixStorageInspectorClient(
            config.inspector_socket,
            config.storage_roles,
            inspector_uid=config.inspector_uid,
            inspector_gid=config.inspector_gid,
            timeout_seconds=config.inspector_timeout_seconds,
            max_age_seconds=config.inspector_max_age_seconds,
            inventory_generation=inventory_generation(config.storage_roles),
        )

    def inspect(self, role_id: str) -> StorageObservation:
        return self.client.inspect(role_id)


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
        self.inspector = inspector or LinuxQuotaInspector(config)
        self._production_inspector = (
            type(self.inspector) is LinuxQuotaInspector
            and type(self.inspector.client) is UnixStorageInspectorClient
        )
        self.verified = False
        self.verified_slot_ids: frozenset[str] = frozenset()
        self.verified_role_ids: frozenset[str] = frozenset()
        self.runtime_containment_available = False
        self.reasons: tuple[str, ...] = ("workspace_quota_unverified",)
        self._mount_identity: tuple[str, str, int] | None = None
        self._mount_identity_lock = threading.Lock()
        self._verification_lock = threading.RLock()
        self._role_identities: dict[str, tuple[int, int, int]] = {}
        self._task_identities: dict[tuple[str, str], tuple[int, int, int]] = {}
        self._runtime_path_identities: dict[tuple[str, Path], tuple[int, int, int]] = {}
        self._role_identity_lock = threading.Lock()
        self.persistent_file_failure_callback: Callable[[str], None] | None = None
        self.persistent_file_failure_code: str | None = None
        self._failed_file_roles: set[str] = set()

    def verify_startup(self) -> tuple[bool, tuple[str, ...]]:
        with self._verification_lock:
            return self._verify_startup()

    def _verify_startup(self) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if self.persistent_file_failure_code is not None:
            reasons.append(self.persistent_file_failure_code)
        if not self.config.quota_slots:
            reasons.append("quota_slot_inventory_missing")
        if not self.config.storage_roles:
            reasons.append("storage_role_inventory_missing")
        if not self._production_inspector:
            reasons.append("storage_inspector_untrusted")
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
        generation = inventory_generation(self.config.storage_roles)
        if hasattr(self.registry, "storage_role_inventory_matches"):
            if any(
                not self.registry.storage_role_inventory_matches(role, generation)
                for role in self.config.storage_roles
            ):
                reasons.append("storage_role_inventory_conflict")
            if {
                row["role_id"] for row in self.registry.storage_role_configurations()
            } != {role.role_id for role in self.config.storage_roles}:
                reasons.append("storage_role_inventory_conflict")
        else:
            reasons.append("storage_role_registry_unavailable")
        observations: dict[str, StorageObservation] = {}
        verified_ids: set[str] = set()
        slot_records = {
            row["slot_id"]: row for row in self.registry.slot_configurations()
        } if hasattr(self.registry, "slot_configurations") else {}
        for role in self.config.storage_roles:
            try:
                observation = self.inspector.inspect(role.role_id)
                self._verify_observation(role, observation)
                self._verify_local_role_path(role, observation)
                self.verify_existing_files(role.role_id)
                observations[role.role_id] = observation
                if role.kind == "task_slot":
                    slot_id = role.role_id.removeprefix("task-")
                    slot = next(item for item in self.config.quota_slots if item.slot_id == slot_id)
                    self._verify_slot_contents(slot, slot_records.get(slot_id))
                    verified_ids.add(slot_id)
            except (
                InspectorFailure, BrokerError, OSError, ValueError, StopIteration,
                AttributeError, TypeError,
            ) as exc:
                reason = getattr(exc, "state", "inconsistent")
                reasons.append(f"storage_role_{reason}:{role.role_id}")
        self.verified_role_ids = frozenset(observations)
        containment_roles = {
            role.role_id for role in self.config.storage_roles
            if role.kind in {"broker_state", "podman_graph", "podman_runtime", "temporary"}
        }
        containment_blockers = {
            "quota_bypass_privilege", "storage_inspector_untrusted",
            "storage_role_inventory_conflict", "storage_role_registry_unavailable",
            "quota_slot_inventory_mismatch", "quota_slot_registry_unavailable",
            "legacy_task_slot_assignment_missing",
        }
        self.runtime_containment_available = (
            self._production_inspector
            and containment_roles <= self.verified_role_ids
            and not containment_roles & self._failed_file_roles
            and not any(reason in containment_blockers for reason in reasons)
            and _process_identity_is_restricted(
                self.config.service_uid, self.config.service_gid,
            )
        )
        if len(observations) == len(self.config.storage_roles) and observations:
            reasons.extend(aggregate_storage_reasons(self.config, observations))
            first = observations[self.config.storage_roles[0].role_id]
            mount_identity = (
                first.filesystem_uuid, first.mount_target, first.mount_id,
            )
            with self._mount_identity_lock:
                if self._mount_identity is None:
                    self._mount_identity = mount_identity
                elif self._mount_identity != mount_identity:
                    reasons.append("storage_mount_identity_changed")
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
            "storage_role_inventory_missing", "storage_inspector_untrusted",
            "storage_role_inventory_conflict", "storage_role_registry_unavailable",
            "storage_filesystem_identity_conflict", "storage_aggregate_headroom",
            "storage_mount_identity_changed",
            "legacy_task_slot_assignment_missing",
        }
        self.verified = bool(verified_ids) and not any(
            reason in blocked_inventory or reason.startswith("storage_role_")
            for reason in self.reasons
        )
        self.verified_slot_ids = frozenset(verified_ids if self.verified else ())
        return self.verified, self.reasons

    def verify_storage_role(self, role_id: str) -> bool:
        with self._verification_lock:
            return self._verify_storage_role(role_id)

    def verify_runtime_path(self, role_id: str, path: Path) -> None:
        with self._verification_lock:
            role = next((role for role in self.config.storage_roles if role.role_id == role_id), None)
            if role is None or not path.is_absolute() or path != Path(os.path.normpath(path)):
                raise BrokerError("runtime_storage_path_invalid", "Runtime path is not canonical.", 503)
            if not path.is_relative_to(role.path):
                raise BrokerError("runtime_storage_path_escape", "Runtime storage escapes its quota domain.", 503)
            with self.open_storage_role(role_id) as root:
                relative = path.relative_to(role.path)
                descriptor = root.open(str(relative), os.O_RDONLY | os.O_NONBLOCK)
                try:
                    info = os.fstat(descriptor)
                    project_id, flags = _project_attributes(descriptor)
                    if (
                        not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                        or info.st_uid != role.owner_uid or info.st_gid != role.owner_gid
                        or info.st_mode & 0o022 or project_id != role.project_id
                        or stat.S_ISDIR(info.st_mode) and not flags & _FS_XFLAG_PROJINHERIT
                        or mount_identity(descriptor)[2] != mount_identity(root.fd)[2]
                    ):
                        raise BrokerError("runtime_storage_path_identity", "Runtime quota path identity is inconsistent.", 503)
                    identity = mount_identity(descriptor)
                    key = (role_id, path)
                    if self._runtime_path_identities.setdefault(key, identity) != identity:
                        raise BrokerError("runtime_storage_path_drift", "Runtime storage path was substituted.", 503)
                    root.revalidate()
                finally:
                    os.close(descriptor)

    def _verify_storage_role(self, role_id: str) -> bool:
        if (
            not self._production_inspector
            or not _process_identity_is_restricted(
                self.config.service_uid, self.config.service_gid,
            )
        ):
            return False
        role = next(
            (item for item in self.config.storage_roles if item.role_id == role_id),
            None,
        )
        if role is None:
            return False
        try:
            observation = self.inspector.inspect(role_id)
            self._verify_observation(role, observation)
            self._verify_local_role_path(role, observation)
            self.verify_existing_files(role_id)
            return True
        except (
            InspectorFailure, BrokerError, OSError, ValueError,
            AttributeError, TypeError,
        ):
            return False

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
            pinned = self._role_identities.get(f"task-{slot.slot_id}")
            if pinned is not None and mount_identity(slot_fd) != pinned:
                raise ValueError("Quota slot was substituted after role verification")
            entries = set(os.listdir(slot_fd))
            if entries - ({task_id} if task_id else set()):
                raise ValueError("Quota slot contains unowned content")
            if task_id and entries:
                task_fd = open_beneath(slot_fd, task_id, os.O_RDONLY | os.O_DIRECTORY)
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

    @staticmethod
    def _verify_observation(role: StorageRole, observation: StorageObservation) -> None:
        if (
            any(type(value) is not int or value < 0 for value in (
                observation.bytes_used, observation.inodes_used,
                observation.filesystem_bytes, observation.filesystem_bytes_available,
                observation.filesystem_inodes, observation.filesystem_inodes_available,
            ))
            or observation.filesystem_bytes_available > observation.filesystem_bytes
            or observation.filesystem_inodes_available > observation.filesystem_inodes
            or observation.role_id != role.role_id
            or observation.filesystem_uuid != role.filesystem_uuid
            or observation.mount_target != str(role.mount_path)
            or observation.filesystem_type != "ext4"
            or observation.quota_mode != "project"
            or not observation.quota_configuration_enabled
            or not observation.inheritance_enabled
            or observation.project_id != role.project_id
            or observation.byte_limit != role.byte_limit
            or observation.inode_limit != role.inode_limit
            or observation.bytes_used > role.byte_limit
            or observation.inodes_used > role.inode_limit
            or observation.owner_uid != role.owner_uid
            or observation.owner_gid != role.owner_gid
        ):
            raise ValueError("Live storage observation differs from the operator inventory")

    def _verify_local_role_path(
        self, role: StorageRole, observation: StorageObservation,
    ) -> None:
        mount_fd, mount_descriptors = _open_directory_chain(role.mount_path)
        role_fd, role_descriptors = _open_directory_chain(role.path)
        try:
            mount = _mount_for_fd(mount_fd)
            actual = _mount_for_fd(role_fd)
            root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                host_root = _mount_for_fd(root_fd)
            finally:
                os.close(root_fd)
            if (
                mount.target != str(role.mount_path)
                or actual.target != str(role.mount_path)
                or mount.mount_id != observation.mount_id
                or actual.mount_id != observation.mount_id
                or actual.filesystem != observation.filesystem_type
                or actual.device != mount.device
                or actual.device == host_root.device
                or not ({"prjquota", "pquota"} & (actual.mount_options | actual.super_options))
            ):
                raise ValueError("Local storage path mount identity differs from inspector evidence")
            _verify_filesystem_device(role.filesystem_uuid, actual.device)
            _verify_directory_ancestors(
                role_descriptors, role.owner_uid,
            )
            _verify_directory_ownership(
                role_fd, expected_uid=role.owner_uid,
                expected_gid=role.owner_gid, writable=True,
            )
            project_id, flags = _project_attributes(role_fd)
            if project_id != role.project_id or not flags & _FS_XFLAG_PROJINHERIT:
                raise ValueError("Storage role project quota inheritance differs from the inventory")
            filesystem = os.fstatvfs(role_fd)
            local = (
                filesystem.f_blocks * filesystem.f_frsize,
                filesystem.f_bavail * filesystem.f_frsize,
                filesystem.f_files,
                filesystem.f_favail,
            )
            observed = (
                observation.filesystem_bytes,
                observation.filesystem_bytes_available,
                observation.filesystem_inodes,
                observation.filesystem_inodes_available,
            )
            if (
                local[0] != observed[0]
                or local[1] < observed[1]
                or local[2] != observed[2]
                or local[3] < observed[3]
            ):
                raise ValueError("Local filesystem capacity differs from live inspection evidence")
            identity = mount_identity(role_fd)
            with self._role_identity_lock:
                if self._role_identities.setdefault(role.role_id, identity) != identity:
                    raise ValueError("Storage role inode or mount identity changed")
        finally:
            for descriptor in reversed(role_descriptors):
                os.close(descriptor)
            for descriptor in reversed(mount_descriptors):
                os.close(descriptor)


    def task_path_for_inspection(self, task_id: str, *, allow_quarantined: bool = False) -> Path:
        """Diagnostic runtime source spelling only; callers must not use it for filesystem I/O."""
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
            role = next(
                item for item in self.config.storage_roles
                if item.kind == "task_slot" and item.role_id == f"task-{slot.slot_id}"
            )
            live = self.inspector.inspect(role.role_id)
            self._verify_observation(role, live)
            self._verify_local_role_path(role, live)
        except (InspectorFailure, BrokerError, OSError, ValueError, StopIteration):
            self.registry.quarantine_slot(task_id)
            raise BrokerError(
                "workspace_slot_mismatch",
                "Fresh quota inspection no longer matches the assigned task slot.",
                409,
            )
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

    def open_storage_role(self, role_id: str) -> Directory:
        role = next(item for item in self.config.storage_roles if item.role_id == role_id)
        try:
            live = self.inspector.inspect(role_id)
        except InspectorFailure as exc:
            raise BrokerError("storage_inspection_unavailable", "Storage role authority cannot be established.", 503) from exc
        self._verify_observation(role, live)
        authority = Directory(role.path, owner_uid=role.owner_uid,
                              owner_gid=role.owner_gid, private=True)
        try:
            actual = _mount_for_fd(authority.fd)
            project_id, flags = _project_attributes(authority.fd)
            if (
                actual.mount_id != live.mount_id
                or actual.target != str(role.mount_path)
                or actual.filesystem != "ext4"
                or project_id != role.project_id
                or not flags & _FS_XFLAG_PROJINHERIT
            ):
                raise BrokerError("workspace_slot_mismatch", "Opened role identity differs from live inventory.", 409)
            _verify_filesystem_device(role.filesystem_uuid, actual.device)
            identity = mount_identity(authority.fd)
            with self._role_identity_lock:
                previous = self._role_identities.setdefault(role_id, identity)
                if previous != identity:
                    raise BrokerError("filesystem_identity_changed", "Storage role was substituted.", 409)
            authority.revalidate()
            authority.file_verifier = lambda descriptor: self.verify_persistent_file(role_id, descriptor)
            authority.file_failure_callback = lambda code: self._persistent_file_failed(role_id, code)
            return authority
        except BaseException:
            authority.close()
            raise

    def verify_persistent_file(self, role_id: str, descriptor: int) -> None:
        role = next(item for item in self.config.storage_roles if item.role_id == role_id)
        try:
            live = self.inspector.inspect(role_id)
            self._verify_observation(role, live)
            info = os.fstat(descriptor)
            actual = _mount_for_fd(descriptor)
            project_id, _ = _project_attributes(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != role.owner_uid or info.st_gid != role.owner_gid
                or info.st_mode & 0o022
                or info.st_mode & 0o7000
                or role.kind != "task_slot" and info.st_mode & 0o077
                or actual.mount_id != live.mount_id
                or actual.target != str(role.mount_path) or actual.filesystem != live.filesystem_type
                or project_id != role.project_id
            ):
                raise BrokerError("filesystem_file_quota_mismatch", "Persistent file does not belong to its verified quota domain.", 503)
            _verify_filesystem_device(role.filesystem_uuid, actual.device)
        except (BrokerError, InspectorFailure, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, BrokerError) else "filesystem_file_quota_unverified"
            self._persistent_file_failed(role_id, code)
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError(code, "Persistent file quota identity cannot be verified.", 503) from exc

    def _persistent_file_failed(self, role_id: str, code: str) -> None:
        first_failure = self.persistent_file_failure_code is None
        if first_failure:
            self.persistent_file_failure_code = code
        self._failed_file_roles.add(role_id)
        self.verified = False
        self.verified_slot_ids = frozenset()
        if first_failure and self.persistent_file_failure_callback is not None:
            self.persistent_file_failure_callback(code)

    def verify_existing_files(self, role_id: str) -> None:
        role = next(item for item in self.config.storage_roles if item.role_id == role_id)
        remaining = role.inode_limit

        def inspect(directory: Directory, depth: int) -> None:
            nonlocal remaining
            if depth > 32:
                raise BrokerError("filesystem_inventory_limit", "Persistent file inventory exceeds its depth bound.", 503)
            for name in os.listdir(directory.fd):
                remaining -= 1
                if remaining < 0:
                    raise BrokerError("filesystem_inventory_limit", "Persistent file inventory exceeds its inode bound.", 503)
                info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    with directory.child_directory(name) as child:
                        inspect(child, depth + 1)
                else:
                    descriptor = directory.open(name, os.O_RDONLY | os.O_NONBLOCK)
                    os.close(descriptor)
            directory.revalidate()

        with self.open_storage_role(role_id) as root:
            inspect(root, 0)

    def open_task_root(self, task_id: str, *, allow_quarantined: bool = False,
                       create: bool = False) -> Directory:
        if not re.fullmatch(r"[a-f0-9]{32}", task_id) or not self.verified:
            raise BrokerError("workspace_quota_unverified", "Task authority is unavailable.", 503)
        assignment = self.registry.task_slot(task_id, allow_quarantined=allow_quarantined)
        slot = next(item for item in self.config.quota_slots if item.slot_id == assignment["slot_id"])
        if slot.slot_id not in self.verified_slot_ids or assignment["path"] != str(slot.path):
            raise BrokerError("workspace_slot_mismatch", "Task slot inventory is inconsistent.", 409)
        task = None
        try:
            with self.open_storage_role(f"task-{slot.slot_id}") as authority:
                current = self.registry.task_slot(task_id, allow_quarantined=allow_quarantined)
                if dict(current) != dict(assignment):
                    raise BrokerError("workspace_slot_mismatch", "Task allocation changed before filesystem access.", 409)
                task = authority.mkdir(task_id) if create else authority.child_directory(task_id)
                info = os.fstat(task.fd)
                project_id, flags = _project_attributes(task.fd)
                if (
                    info.st_uid != self.config.service_uid or info.st_gid != self.config.service_gid
                    or stat.S_IMODE(info.st_mode) != 0o700 or project_id != slot.project_id
                    or not flags & _FS_XFLAG_PROJINHERIT
                ):
                    raise BrokerError("workspace_slot_mismatch", "Task directory quota identity is unsafe.", 409)
                with self._role_identity_lock:
                    key = (task_id, assignment["allocation_id"])
                    identity = mount_identity(task.fd)
                    if self._task_identities.setdefault(key, identity) != identity:
                        raise BrokerError("filesystem_identity_changed", "Task root was substituted.", 409)
                task.revalidate()
                return task
        except (OSError, ValueError, BrokerError) as exc:
            if task is not None:
                task.close()
            self.registry.quarantine_slot(task_id)
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError(
                "workspace_path_unverified",
                "Task directory cannot be safely used; evidence is preserved.",
                503,
            ) from exc

    def create_task_root(self, task_id: str) -> Directory:
        return self.open_task_root(task_id, create=True)

    def clean_task_root(self, task_id: str) -> None:
        assignment = self.registry.task_slot(task_id)
        try:
            with self.open_storage_role(f"task-{assignment['slot_id']}") as slot:
                try:
                    root = slot.child_directory(task_id)
                except FileNotFoundError:
                    return
                with root:
                    if self.registry.task_slot(task_id)["allocation_id"] != assignment["allocation_id"]:
                        raise BrokerError("workspace_slot_mismatch", "Task allocation changed during cleanup.", 409)
                    project_id, flags = _project_attributes(root.fd)
                    if project_id != assignment["project_id"] or not flags & _FS_XFLAG_PROJINHERIT:
                        raise BrokerError("workspace_slot_mismatch", "Cleanup quota identity is unavailable.", 409)
                    with self._role_identity_lock:
                        key = (task_id, assignment["allocation_id"])
                        identity = mount_identity(root.fd)
                        if self._task_identities.setdefault(key, identity) != identity:
                            raise BrokerError("filesystem_identity_changed", "Cleanup task root was substituted.", 409)
                    remove_tree(root)
        except (BrokerError, OSError, ValueError):
            self.registry.quarantine_slot(task_id)
            raise

    def remove_empty_task_root(self, task_id: str) -> None:
        assignment = self.registry.task_slot(task_id, allow_quarantined=True)
        with self.open_storage_role(f"task-{assignment['slot_id']}") as slot:
            try:
                task = slot.child_directory(task_id)
            except FileNotFoundError:
                return
            with task:
                project_id, flags = _project_attributes(task.fd)
                if (
                    project_id != assignment["project_id"]
                    or not flags & _FS_XFLAG_PROJINHERIT
                    or stat.S_IMODE(os.fstat(task.fd).st_mode) != 0o700
                ):
                    raise BrokerError("workspace_slot_mismatch", "Release task quota identity is unsafe.", 409)
                with self._role_identity_lock:
                    identity = self._task_identities.get((task_id, assignment["allocation_id"]))
                    if identity is not None and mount_identity(task.fd) != identity:
                        raise BrokerError("filesystem_identity_changed", "Release task root was substituted.", 409)
                if os.listdir(task.fd):
                    raise BrokerError("workspace_cleanup_incomplete", "Task evidence is not empty.", 409)
                task.revalidate()
                if self.registry.task_slot(task_id, allow_quarantined=True)["allocation_id"] != assignment["allocation_id"]:
                    raise BrokerError("workspace_slot_mismatch", "Task assignment changed during cleanup.", 409)
                os.rmdir(task_id, dir_fd=slot.fd)
                os.fsync(slot.fd)

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
                self.remove_empty_task_root(task_id)
                self.registry.release_slot(
                    task_id,
                    str(slot_record["release_evidence"]),
                )
            except (BrokerError, OSError):
                self.registry.quarantine_slot(task_id)
        self.verify_startup()


def aggregate_storage_reasons(
    config: BrokerConfig,
    observations: dict[str, StorageObservation],
) -> tuple[str, ...]:
    if not config.storage_roles or set(observations) != {
        role.role_id for role in config.storage_roles
    }:
        return ("storage_observations_incomplete",)
    if (
        len({(role.filesystem_uuid, role.project_id) for role in config.storage_roles})
        != len(config.storage_roles)
        or any(left.path.is_relative_to(right.path)
               for left in config.storage_roles for right in config.storage_roles
               if left.role_id != right.role_id)
    ):
        return ("storage_quota_domain_conflict",)
    try:
        for role in config.storage_roles:
            QuotaSlotManager._verify_observation(role, observations[role.role_id])
    except ValueError:
        return ("storage_quota_observation_conflict",)
    first = observations[config.storage_roles[0].role_id]
    identity = (
        first.filesystem_uuid, first.mount_target, first.mount_id,
        first.filesystem_bytes, first.filesystem_inodes,
    )
    if any(
        (
            item.filesystem_uuid, item.mount_target, item.mount_id,
            item.filesystem_bytes, item.filesystem_inodes,
        ) != identity
        for item in observations.values()
    ):
        return ("storage_filesystem_identity_conflict",)
    remaining_bytes = sum(
        role.byte_limit - observations[role.role_id].bytes_used
        for role in config.storage_roles
    )
    remaining_inodes = sum(
        role.inode_limit - observations[role.role_id].inodes_used
        for role in config.storage_roles
    )
    if (
        first.filesystem_bytes < config.quota_volume_bytes
        or first.filesystem_inodes < config.quota_volume_inodes
        or min(item.filesystem_bytes_available for item in observations.values())
        < remaining_bytes + config.quota_reserve_bytes + 256 * 1024**2
        or min(item.filesystem_inodes_available for item in observations.values())
        < remaining_inodes + config.quota_reserve_inodes + 4096
    ):
        return ("storage_aggregate_headroom",)
    return ()


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
            current = open_beneath(current, component, os.O_RDONLY | os.O_DIRECTORY, no_xdev=False)
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
    root_device = os.stat("/").st_dev
    if f"{os.major(root_device)}:{os.minor(root_device)}" == device:
        raise ValueError("Broker storage must not use the host root filesystem")
    if (Path("/sys/dev/block") / device / "loop" / "backing_file").exists():
        raise ValueError("Loop-backed filesystems are development-only and cannot satisfy readiness")


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


def _verify_directory_ancestors(
    descriptors: list[int], expected_uid: int,
) -> None:
    for descriptor in descriptors[1:-1]:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, expected_uid}
            or info.st_mode & 0o022
        ):
            raise ValueError("Storage path ancestor is not protected from substitution")


def _project_attributes(descriptor: int) -> tuple[int, int]:
    buffer = bytearray(28)
    fcntl.ioctl(descriptor, _FS_IOC_FSGETXATTR, buffer, True)
    flags, _, _, project_id, _, _ = struct.unpack("=IIIII8s", buffer)
    return project_id, flags
