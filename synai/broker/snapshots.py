from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import stat
import time
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from synai.broker.protocol import BrokerError, TaskClaim, canonical_json
from synai.broker.filesystem import Directory, directory_access, read_file, walk_files, write_new


MAX_FILES = 500
MAX_FILE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_PATH_CHARS = 512
MAX_PATH_DEPTH = 16
MAX_DIRECTORIES = 500
MAX_TREE_INODES = 1002
MAX_IMPORT_INODES = 2006
MAX_IMPORT_SECONDS = 60
MAX_EXPORT_SECONDS = 60
MAX_ALLOCATED_STAGING_BYTES = 144 * 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")


@dataclass(frozen=True)
class SnapshotProvenance:
    task_id: str
    project_id: str
    snapshot_id: str
    snapshot_digest: str
    lease_generation: int
    import_operation_id: str

    @classmethod
    def from_claim(cls, claim: TaskClaim) -> SnapshotProvenance:
        return cls(
            task_id=claim.task_id,
            project_id=claim.project_id,
            snapshot_id=claim.snapshot_id,
            snapshot_digest=claim.snapshot_digest,
            lease_generation=claim.lease_generation,
            import_operation_id=claim.operation_id,
        )

    def validate(self) -> None:
        if any(not _ID.fullmatch(value) for value in (
            self.task_id, self.project_id, self.snapshot_id, self.import_operation_id,
        )) or not _HASH.fullmatch(self.snapshot_digest) or (
            type(self.lease_generation) is not int or self.lease_generation < 1
        ):
            raise BrokerError("snapshot_provenance_invalid", "Task snapshot provenance is invalid.", 422)

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> SnapshotProvenance:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerError("snapshot_provenance_invalid", "Task snapshot provenance schema is invalid.", 409)
        try:
            result = cls(**value)
            result.validate()
            return result
        except (TypeError, ValueError) as exc:
            raise BrokerError("snapshot_provenance_invalid", "Task snapshot provenance is invalid.", 409) from exc


def validate_relative_path(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS
        or "\\" in value or "\x00" in value or value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise BrokerError("snapshot_path_invalid", "Snapshot path is not a safe relative path.", 422)
    path = PurePosixPath(value)
    if path.as_posix() != value or len(path.parts) > MAX_PATH_DEPTH or any(part in {"", ".", ".."} for part in path.parts):
        raise BrokerError("snapshot_path_invalid", "Snapshot path is not a safe relative path.", 422)
    if unicodedata.normalize("NFC", value) != value:
        raise BrokerError("snapshot_path_invalid", "Snapshot path is not canonical Unicode.", 422)
    return value


def validate_snapshot(
    manifest: object,
    expected_snapshot: str,
    expected_digest: str,
    files: object,
    *,
    expected_project: str | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "project_id", "source_device_id", "workspace_binding_id",
        "snapshot_id", "created_at", "files", "total_bytes", "source",
    }:
        raise BrokerError("snapshot_manifest_invalid", "Snapshot manifest fields are invalid.", 422)
    if manifest["schema_version"] != 1 or manifest["snapshot_id"] != expected_snapshot:
        raise BrokerError("snapshot_identity_mismatch", "Snapshot identity does not match the authorized claim.", 403)
    if expected_project is not None and manifest["project_id"] != expected_project:
        raise BrokerError("snapshot_project_mismatch", "Snapshot belongs to another logical project.", 403)
    body = canonical_json(manifest)
    if not _HASH.fullmatch(expected_digest) or hashlib.sha256(body).hexdigest() != expected_digest:
        raise BrokerError("snapshot_manifest_digest_invalid", "Snapshot manifest digest does not match.", 422)
    entries = manifest["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES or not isinstance(files, dict) or len(files) != len(entries):
        raise BrokerError("snapshot_manifest_invalid", "Snapshot file list is outside its bounds.", 422)
    normalized: set[str] = set()
    total = 0
    declared: set[str] = set()
    directory_names: set[str] = set()
    for entry in entries:
        if deadline is not None and time.monotonic() > deadline:
            raise BrokerError("snapshot_deadline", "Snapshot validation exceeded its deadline.", 503)
        if not isinstance(entry, dict) or set(entry) != {"path", "file_type", "size_bytes", "sha256"}:
            raise BrokerError("snapshot_manifest_invalid", "Snapshot file entry is invalid.", 422)
        path = validate_relative_path(entry["path"])
        normalized_path = unicodedata.normalize("NFC", path).casefold()
        if normalized_path in normalized or path in declared:
            raise BrokerError("snapshot_path_collision", "Snapshot paths collide.", 422)
        normalized.add(normalized_path)
        declared.add(path)
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in declared:
                raise BrokerError("snapshot_path_collision", "A file path is also used as a directory.", 422)
            directory_names.add(parent)
        size = entry["size_bytes"]
        digest = entry["sha256"]
        if (
            entry["file_type"] != "regular" or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)
            or path not in files or not isinstance(files[path], bytes)
        ):
            raise BrokerError("snapshot_file_invalid", "Snapshot contains an unsupported file.", 422)
        data = files[path]
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest or b"\0" in data:
            raise BrokerError("snapshot_file_digest_invalid", "Snapshot file failed integrity validation.", 422)
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BrokerError("snapshot_file_invalid", "Snapshot files must contain UTF-8 text.", 422) from exc
        total += size
    if total > MAX_SNAPSHOT_BYTES or manifest["total_bytes"] != total or set(files) != declared:
        raise BrokerError("snapshot_size_invalid", "Snapshot total size or file set is inconsistent.", 422)
    if len(directory_names) > MAX_DIRECTORIES or 1 + len(entries) + len(directory_names) > MAX_IMPORT_INODES:
        raise BrokerError("snapshot_inode_limit", "Snapshot tree exceeds its inode bound.", 413)
    for path in declared:
        if any(path.startswith(other + "/") for other in declared if other != path):
            raise BrokerError("snapshot_path_collision", "A file path conflicts with a descendant.", 422)
    return manifest


def stage_snapshot(
    task_root: Path | Directory,
    manifest: dict[str, Any],
    files: dict[str, bytes],
    provenance: SnapshotProvenance,
    *,
    deadline: float | None = None,
) -> Path:
    provenance.validate()
    if (
        manifest.get("project_id") != provenance.project_id
        or manifest.get("snapshot_id") != provenance.snapshot_id
        or hashlib.sha256(canonical_json(manifest)).hexdigest() != provenance.snapshot_digest
    ):
        raise BrokerError("snapshot_provenance_mismatch", "Import does not match its authorized provenance.", 403)
    if deadline is None:
        deadline = time.monotonic() + MAX_IMPORT_SECONDS
    validate_snapshot(
        manifest, provenance.snapshot_id, provenance.snapshot_digest, files,
        expected_project=provenance.project_id, deadline=deadline,
    )
    with directory_access(task_root) as root:
        if os.listdir(root.fd):
            raise BrokerError("task_workspace_exists", "Assigned task directory is not empty.", 409)
        required = (manifest["total_bytes"] * 2) + (4 * 1024 * 1024)
        available = os.fstatvfs(root.fd).f_bavail * os.fstatvfs(root.fd).f_frsize
        if available < required:
            raise BrokerError("snapshot_storage_exhausted", "Task workspace has insufficient available storage.", 507)
        staging_name = f".importing-{provenance.import_operation_id}"
        with root.mkdir(staging_name) as staging:
            write_new(staging, "manifest.json", canonical_json(manifest), 0o400)
            write_new(staging, "provenance.json", canonical_json(provenance.to_dict()), 0o400)
            with staging.mkdir("snapshot") as source, staging.mkdir("workspace") as work:
                state = {"allocated_bytes": 0, "inodes": 6}
                for entry in manifest["files"]:
                    if time.monotonic() > deadline:
                        raise BrokerError("snapshot_deadline", "Snapshot import exceeded its deadline.", 503)
                    path = validate_relative_path(entry["path"])
                    _stage_file(source, path, files[path], 0o400, state, deadline)
                    _stage_file(work, path, files[path], 0o600, state, deadline)
                source_allocated, _ = _verify_tree(
                    source, manifest, deadline, allocated_limit=MAX_ALLOCATED_STAGING_BYTES,
                )
                work_allocated, _ = _verify_tree(
                    work, manifest, deadline, allocated_limit=MAX_ALLOCATED_STAGING_BYTES,
                )
                metadata_allocated = (
                    os.fstat(root.fd).st_blocks
                    + os.fstat(staging.fd).st_blocks
                ) * 512
                for metadata in ("manifest.json", "provenance.json"):
                    metadata_fd = staging.open(metadata, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        metadata_allocated += os.fstat(metadata_fd).st_blocks * 512
                    finally:
                        os.close(metadata_fd)
                if source_allocated + work_allocated + metadata_allocated > MAX_ALLOCATED_STAGING_BYTES:
                    raise BrokerError("snapshot_storage_limit", "Task staging allocated blocks exceed their hard bound.", 413)
                os.fsync(staging.fd)
            # Keep workspace publication last. Until it exists the task cannot become ready.
            for name in ("snapshot", "manifest.json", "provenance.json"):
                os.rename(name, name, src_dir_fd=staging.fd, dst_dir_fd=root.fd)
            os.rename("workspace", "workspace", src_dir_fd=staging.fd, dst_dir_fd=root.fd)
            with root.child_directory("snapshot") as source:
                _make_immutable(source)
            os.fsync(root.fd)
            os.rmdir(staging_name, dir_fd=root.fd)
            os.fsync(root.fd)
        return root.path / "workspace"


def _stage_file(
    root: Directory,
    path: str,
    data: bytes,
    mode: int,
    state: dict[str, int] | None = None,
    deadline: float | None = None,
) -> None:
    if state is None:
        state = {"allocated_bytes": 0, "inodes": 0}
    parts = PurePosixPath(path).parts
    if len(parts) == 1:
        if deadline is not None and time.monotonic() > deadline:
            raise BrokerError("snapshot_deadline", "Snapshot import exceeded its deadline.", 503)
        descriptor = root.open(parts[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            view = memoryview(data)
            while view:
                if deadline is not None and time.monotonic() > deadline:
                    raise BrokerError("snapshot_deadline", "Snapshot import exceeded its deadline.", 503)
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("Snapshot staging write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(data):
                raise BrokerError("snapshot_file_identity_changed", "Staged file identity or size changed.", 409)
            state["allocated_bytes"] += info.st_blocks * 512
            state["inodes"] += 1
            if state["allocated_bytes"] > MAX_ALLOCATED_STAGING_BYTES or state["inodes"] > MAX_IMPORT_INODES:
                raise BrokerError("snapshot_storage_limit", "Allocated task storage exceeds its hard bound.", 413)
        finally:
            os.close(descriptor)
        root.revalidate()
        os.fsync(root.fd)
        return
    try:
        child = root.mkdir(parts[0])
        if state is not None:
            state["inodes"] += 1
            if state["inodes"] > MAX_IMPORT_INODES:
                child.close()
                raise BrokerError("snapshot_inode_limit", "Task workspace exceeds its inode bound.", 413)
    except FileExistsError:
        child = root.child_directory(parts[0])
    with child:
        _stage_file(child, "/".join(parts[1:]), data, mode, state, deadline)


def _make_immutable(directory: Directory) -> None:
    for name in os.listdir(directory.fd):
        info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with directory.child_directory(name) as child:
                _make_immutable(child)
        elif not stat.S_ISREG(info.st_mode):
            raise BrokerError("snapshot_file_invalid", "Snapshot entry changed during staging.", 409)
    directory.revalidate()
    os.fchmod(directory.fd, 0o500)
    os.fsync(directory.fd)


def _verify_tree(
    directory: Directory,
    manifest: dict[str, Any],
    deadline: float,
    *,
    allocated_limit: int,
) -> tuple[int, int]:
    expected = {entry["path"]: entry for entry in manifest["files"]}
    files: set[str] = set()
    state = {"directories": 0, "files": 0, "allocated_bytes": 0}
    for relative, data, info in _walk_verified(directory, deadline, state=state):
        files.add(relative)
        entry = expected.get(relative)
        if (
            entry is None or len(data) != entry["size_bytes"]
            or hashlib.sha256(data).hexdigest() != entry["sha256"]
            or info.st_nlink != 1
        ):
            raise BrokerError("snapshot_staging_mismatch", "Staged tree differs from its authenticated manifest.", 409)
    inodes = 1 + state["directories"] + state["files"]
    allocated = state["allocated_bytes"]
    if files != set(expected) or allocated > allocated_limit or inodes > MAX_TREE_INODES:
        raise BrokerError("snapshot_staging_mismatch", "Staged tree exceeds or differs from its authenticated manifest.", 409)
    return allocated, inodes


def verify_staged_snapshot(
    snapshot_directory: Path | Directory,
    manifest: dict[str, Any],
    *,
    deadline: float | None = None,
) -> tuple[int, int]:
    if deadline is None:
        deadline = time.monotonic() + MAX_EXPORT_SECONDS
    with directory_access(snapshot_directory) as snapshot:
        return _verify_tree(
            snapshot, manifest, deadline,
            allocated_limit=MAX_ALLOCATED_STAGING_BYTES,
        )


def _walk_verified(
    directory: Directory,
    deadline: float,
    prefix: str = "",
    *,
    state: dict[str, int] | None = None,
) -> Iterator[tuple[str, bytes, os.stat_result]]:
    if state is None:
        state = {"directories": 0, "files": 0, "allocated_bytes": 0}
    if time.monotonic() > deadline:
        raise BrokerError("snapshot_deadline", "Filesystem traversal exceeded its deadline.", 503)
    directory.revalidate()
    state["allocated_bytes"] = state.get("allocated_bytes", 0) + os.fstat(directory.fd).st_blocks * 512
    names = sorted(os.listdir(directory.fd))
    if len(names) > MAX_IMPORT_INODES:
        raise BrokerError("snapshot_inode_limit", "Filesystem traversal exceeds its inode bound.", 413)
    for name in names:
        validate_relative_path(prefix + name)
        before = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            if (
                before.st_uid != directory.owner_uid
                or before.st_mode & 0o7022
                or before.st_nlink < 2
            ):
                raise BrokerError("snapshot_directory_metadata_invalid", "Task tree directory has unsafe ownership or mode.", 422)
            state["directories"] += 1
            if state["directories"] > MAX_DIRECTORIES:
                raise BrokerError("snapshot_inode_limit", "Filesystem traversal exceeds its directory bound.", 413)
            with directory.child_directory(name) as child:
                yield from _walk_verified(child, deadline, prefix + name + "/", state=state)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            state["files"] += 1
            if state["files"] > MAX_FILES:
                raise BrokerError("snapshot_inode_limit", "Filesystem traversal exceeds its file bound.", 413)
            descriptor = directory.open(name, os.O_RDONLY | os.O_NONBLOCK)
            try:
                opened = os.fstat(descriptor)
                identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                if (
                    not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_uid != directory.owner_uid
                    or opened.st_mode & 0o7022
                    or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != identity
                    or opened.st_size > MAX_FILE_BYTES
                ):
                    raise BrokerError("snapshot_file_identity_changed", "Task file changed during descriptor acquisition.", 409)
                try:
                    if os.listxattr(descriptor):
                        raise BrokerError("snapshot_file_metadata_invalid", "Task files with unapproved extended metadata are rejected.", 422)
                except (AttributeError, OSError) as exc:
                    raise BrokerError("snapshot_file_metadata_unavailable", "Task file metadata could not be verified.", 503) from exc
                content = bytearray()
                while len(content) <= MAX_FILE_BYTES:
                    if time.monotonic() > deadline:
                        raise BrokerError("snapshot_deadline", "Filesystem read exceeded its deadline.", 503)
                    chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - len(content)))
                    if not chunk:
                        break
                    content.extend(chunk)
                after = os.fstat(descriptor)
                current = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                if (
                    len(content) != opened.st_size
                    or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise BrokerError("snapshot_file_identity_changed", "Task file changed while it was read.", 409)
                state["allocated_bytes"] = state.get("allocated_bytes", 0) + opened.st_blocks * 512
                yield prefix + name, bytes(content), opened
            finally:
                os.close(descriptor)
        else:
            raise BrokerError("snapshot_file_invalid", "Task tree contains a link or unsupported file type.", 422)


def load_staged_manifest(task_root: Path | Directory, expected_digest: str) -> dict[str, Any]:
    try:
        with directory_access(task_root) as root:
            data = read_file(root, "manifest.json", 2 * 1024 * 1024)
        manifest = json.loads(data)
        if hashlib.sha256(canonical_json(manifest)).hexdigest() != expected_digest:
            raise ValueError("Staged snapshot manifest digest changed")
        return manifest
    except (OSError, ValueError, TypeError) as exc:
        raise BrokerError("snapshot_integrity_error", "Staged immutable snapshot failed integrity validation.", 409) from exc


def load_staged_provenance(
    task_root: Path | Directory,
    expected: SnapshotProvenance,
) -> dict[str, object]:
    expected.validate()
    try:
        with directory_access(task_root) as root:
            data = read_file(root, "provenance.json", 4096)
        value = SnapshotProvenance.from_dict(json.loads(data))
        if (
            value.task_id != expected.task_id
            or value.project_id != expected.project_id
            or value.snapshot_id != expected.snapshot_id
            or value.snapshot_digest != expected.snapshot_digest
            or value.lease_generation != expected.lease_generation
        ):
            raise ValueError("Task workspace provenance differs from the current task authority")
        return value.to_dict()
    except (OSError, ValueError, TypeError) as exc:
        raise BrokerError("snapshot_provenance_mismatch", "Task workspace provenance is unavailable or stale.", 409) from exc


def changed_files(
    workspace: Path | Directory,
    manifest: dict[str, Any],
    maximum: int = MAX_SNAPSHOT_BYTES,
    output_limit: int = 1024 * 1024,
    *,
    deadline: float | None = None,
) -> list[dict[str, object]]:
    if deadline is None:
        deadline = time.monotonic() + MAX_EXPORT_SECONDS
    before = {entry["path"]: entry for entry in manifest["files"]}
    seen: set[str] = set()
    changes: list[dict[str, object]] = []
    total = 0
    encoded_output = 0
    normalized_seen: set[str] = set()
    if len(before) > MAX_FILES:
        raise BrokerError("artifact_limit", "Snapshot manifest exceeds the file bound.", 413)
    with directory_access(workspace) as root:
        state = {"directories": 0, "files": 0, "allocated_bytes": 0}
        for relative, data, info in _walk_verified(root, deadline, state=state):
            relative = validate_relative_path(relative)
            normalized = unicodedata.normalize("NFC", relative).casefold()
            if relative in seen or normalized in normalized_seen:
                raise BrokerError("artifact_path_collision", "Task output contains duplicate paths.", 422)
            normalized_seen.add(normalized)
            if total + len(data) > maximum or info.st_blocks * 512 > MAX_ALLOCATED_STAGING_BYTES:
                raise BrokerError("artifact_limit", "Task artifacts exceed the configured bound.", 413)
            if len(data) > MAX_FILE_BYTES:
                raise BrokerError("artifact_limit", "Task artifact file exceeds its configured bound.", 413)
            total += len(data)
            digest = hashlib.sha256(data).hexdigest()
            old = before.get(relative)
            seen.add(relative)
            if old is None or digest != old["sha256"]:
                encoded = base64.b64encode(data).decode("ascii")
                entry = {
                    "path": relative, "preimage_sha256": None if old is None else old["sha256"],
                    "postimage_sha256": digest, "size_bytes": len(data),
                    "artifact_base64": encoded,
                }
                encoded_output += len(canonical_json(entry))
                if encoded_output > output_limit:
                    raise BrokerError("artifact_output_limit", "Changed-file artifacts exceed the operation output limit.", 413)
                changes.append(entry)
    for path, entry in before.items():
        if path not in seen:
            changes.append({
                "path": path, "preimage_sha256": entry["sha256"],
                "postimage_sha256": None, "size_bytes": 0,
            })
            encoded_output += len(canonical_json(changes[-1]))
            if encoded_output > output_limit:
                raise BrokerError("artifact_output_limit", "Changed-file manifest exceeds the operation output limit.", 413)
    if 1 + state["directories"] + state["files"] > MAX_TREE_INODES:
        raise BrokerError("artifact_limit", "Task output exceeds its inode bound.", 413)
    if state["allocated_bytes"] > min(MAX_ALLOCATED_STAGING_BYTES, maximum):
        raise BrokerError("artifact_limit", "Allocated task output exceeds its configured bound.", 413)
    return sorted(changes, key=lambda item: str(item["path"]))


def build_artifact_manifest(
    workspace: Path | Directory,
    source_manifest: dict[str, Any],
    provenance: SnapshotProvenance,
    *,
    maximum: int,
    output_limit: int,
    deadline: float | None = None,
) -> dict[str, object]:
    provenance.validate()
    if hashlib.sha256(canonical_json(source_manifest)).hexdigest() != provenance.snapshot_digest:
        raise BrokerError("snapshot_provenance_mismatch", "Artifact source manifest differs from its provenance.", 409)
    changes = changed_files(
        workspace, source_manifest, maximum, output_limit, deadline=deadline,
    )
    envelope: dict[str, object] = {
        "schema_version": 1,
        "provenance": provenance.to_dict(),
        "source_manifest_sha256": provenance.snapshot_digest,
        "changed_files": changes,
    }
    envelope["artifact_manifest_sha256"] = hashlib.sha256(canonical_json(changes)).hexdigest()
    encoded = canonical_json(envelope)
    if len(encoded) > output_limit:
        raise BrokerError("artifact_output_limit", "Task artifact manifest exceeds its bound.", 413)
    envelope["envelope_sha256"] = hashlib.sha256(encoded).hexdigest()
    return envelope


class OfflineLifecycleState(StrEnum):
    EMPTY = "empty"
    SNAPSHOT_PREPARED = "snapshot_prepared"
    STORAGE_ALLOCATED = "storage_allocated"
    CONTAINER_CREATED = "container_created"
    INPUT_COPIED = "input_copied"
    DESTINATION_VERIFIED = "destination_verified"
    STARTED_IN_MODEL = "started_in_model"
    TERMINATED_VERIFIED = "terminated_verified"
    OUTPUT_EXPORTED = "output_exported"
    QUARANTINED = "quarantined"
    RELEASED = "released"


class OfflineWorkspaceLifecycle:
    """Detached fake-only lifecycle contract; it never grants runtime readiness."""

    def __init__(self, provenance: SnapshotProvenance) -> None:
        provenance.validate()
        self.provenance = provenance
        self.state = OfflineLifecycleState.EMPTY
        self.container_id: str | None = None
        self.destination_identity: str | None = None
        self.termination_evidence: str | None = None
        self.artifact_digest: str | None = None
        self.assurance_lost = False

    def advance(
        self,
        expected: OfflineLifecycleState,
        target: OfflineLifecycleState,
        *,
        evidence: str | None = None,
    ) -> None:
        transitions = {
            OfflineLifecycleState.EMPTY: {OfflineLifecycleState.SNAPSHOT_PREPARED},
            OfflineLifecycleState.SNAPSHOT_PREPARED: {OfflineLifecycleState.STORAGE_ALLOCATED},
            OfflineLifecycleState.STORAGE_ALLOCATED: {OfflineLifecycleState.CONTAINER_CREATED},
            OfflineLifecycleState.CONTAINER_CREATED: {OfflineLifecycleState.INPUT_COPIED},
            OfflineLifecycleState.INPUT_COPIED: {OfflineLifecycleState.DESTINATION_VERIFIED},
            OfflineLifecycleState.DESTINATION_VERIFIED: {OfflineLifecycleState.STARTED_IN_MODEL},
            OfflineLifecycleState.STARTED_IN_MODEL: {OfflineLifecycleState.TERMINATED_VERIFIED},
            OfflineLifecycleState.TERMINATED_VERIFIED: {OfflineLifecycleState.OUTPUT_EXPORTED},
            OfflineLifecycleState.OUTPUT_EXPORTED: {OfflineLifecycleState.RELEASED},
        }
        if (
            self.state != expected or self.assurance_lost
            or target not in transitions.get(self.state, set())
            or self.state in {OfflineLifecycleState.QUARANTINED, OfflineLifecycleState.RELEASED}
        ):
            self.quarantine()
            raise BrokerError("workspace_lifecycle_uncertain", "Offline lifecycle evidence is stale or out of order.", 409)
        if target in {
            OfflineLifecycleState.SNAPSHOT_PREPARED,
            OfflineLifecycleState.STORAGE_ALLOCATED,
            OfflineLifecycleState.CONTAINER_CREATED,
            OfflineLifecycleState.INPUT_COPIED,
            OfflineLifecycleState.DESTINATION_VERIFIED,
            OfflineLifecycleState.STARTED_IN_MODEL,
            OfflineLifecycleState.TERMINATED_VERIFIED,
            OfflineLifecycleState.OUTPUT_EXPORTED,
            OfflineLifecycleState.RELEASED,
        } and not evidence:
            self.quarantine()
            raise BrokerError("workspace_lifecycle_evidence_missing", "Lifecycle transition lacks evidence.", 409)
        if target == OfflineLifecycleState.STARTED_IN_MODEL:
            if (
                self.state != OfflineLifecycleState.DESTINATION_VERIFIED
                or not self.destination_identity or evidence != self.container_id
            ):
                self.quarantine()
                raise BrokerError("workspace_start_unverified", "Offline start lacks destination-verification evidence.", 409)
        if target == OfflineLifecycleState.TERMINATED_VERIFIED and not evidence:
            self.quarantine()
            raise BrokerError("workspace_termination_unverified", "Termination requires exact external evidence.", 409)
        self.state = target
        if target == OfflineLifecycleState.CONTAINER_CREATED:
            self.container_id = evidence
        elif target == OfflineLifecycleState.DESTINATION_VERIFIED:
            self.destination_identity = evidence
        elif target == OfflineLifecycleState.TERMINATED_VERIFIED:
            self.termination_evidence = evidence
        elif target == OfflineLifecycleState.OUTPUT_EXPORTED:
            self.artifact_digest = evidence

    def lose_assurance(self) -> None:
        self.assurance_lost = True
        self.quarantine()

    def quarantine(self) -> None:
        self.state = OfflineLifecycleState.QUARANTINED

    @property
    def production_execution_authorized(self) -> bool:
        return False
