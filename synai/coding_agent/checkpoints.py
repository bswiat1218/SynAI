from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from synai.coding_agent.changes import SnapshotStore
from synai.config import Settings
from synai.execution_backend import validate_workspace
from synai.intelligence import RepositoryIndex
from synai.models import Session
from synai.storage import ConversationStorage, checked_path, write_private_json


_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MAX_PATHS = 64
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 128 * 1024
_MAX_CAPTURE_SECONDS = 10.0
_MAX_CHECKPOINTS = 128


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    path = Path(value)
    return (
        not path.is_absolute() and "\\" not in value and "\x00" not in value
        and path.as_posix() == value and all(part not in {"", ".", ".."} for part in path.parts)
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CheckpointFile:
    path: str
    exists: bool
    sha256: str | None
    snapshot_ref: str | None
    size_bytes: int
    expected_exists: bool
    expected_sha256: str | None
    complete: bool
    limitation: str | None = None

    def validate(self) -> None:
        if not _safe_path(self.path):
            raise ValueError("Invalid checkpoint path")
        if type(self.exists) is not bool or type(self.expected_exists) is not bool:
            raise ValueError("Invalid checkpoint file existence")
        for digest in (self.sha256, self.snapshot_ref, self.expected_sha256):
            if digest is not None and not _HASH.fullmatch(digest):
                raise ValueError("Invalid checkpoint digest")
        if self.exists != (self.sha256 is not None and self.snapshot_ref == self.sha256):
            raise ValueError("Checkpoint file content reference is inconsistent")
        if not self.exists and (self.sha256 is not None or self.snapshot_ref is not None):
            raise ValueError("Missing checkpoint file cannot reference content")
        if self.expected_exists != (self.expected_sha256 is not None):
            raise ValueError("Checkpoint expected state is inconsistent")
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= 1_048_576:
            raise ValueError("Invalid checkpoint file size")
        if type(self.complete) is not bool:
            raise ValueError("Invalid checkpoint file completeness")
        if self.limitation is not None and (
            not isinstance(self.limitation, str) or len(self.limitation) > 512
        ):
            raise ValueError("Invalid checkpoint limitation")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return dict(vars(self))

    @classmethod
    def from_dict(cls, value: object) -> CheckpointFile:
        keys = {
            "path", "exists", "sha256", "snapshot_ref", "size_bytes",
            "expected_exists", "expected_sha256", "complete", "limitation",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid checkpoint file fields")
        result = cls(**value)
        result.validate()
        return result


@dataclass
class CheckpointRecord:
    checkpoint_id: str
    owner_id: str
    task_id: str | None
    workspace_identity: str
    backend_identity: str
    repository_identity: dict[str, str | None] | None
    created_at: str
    files: list[CheckpointFile]
    total_size_bytes: int
    complete: bool
    integrity_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "owner_id": self.owner_id,
            "task_id": self.task_id,
            "workspace_identity": self.workspace_identity,
            "backend_identity": self.backend_identity,
            "repository_identity": self.repository_identity,
            "created_at": self.created_at,
            "files": [item.to_dict() for item in self.files],
            "total_size_bytes": self.total_size_bytes,
            "complete": self.complete,
        }

    def seal(self) -> None:
        self.integrity_sha256 = hashlib.sha256(_canonical(self.payload())).hexdigest()

    def validate(self) -> None:
        if not _ID.fullmatch(self.checkpoint_id) or not _ID.fullmatch(self.owner_id):
            raise ValueError("Invalid checkpoint identity")
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id or len(self.task_id) > 128
        ):
            raise ValueError("Invalid checkpoint task identity")
        if not isinstance(self.workspace_identity, str) or not self.workspace_identity:
            raise ValueError("Invalid checkpoint workspace identity")
        if self.backend_identity not in {"host", "sandbox"}:
            raise ValueError("Invalid checkpoint backend identity")
        if self.repository_identity is not None and (
            not isinstance(self.repository_identity, dict)
            or set(self.repository_identity) != {"root", "git_directory"}
            or any(
                value is not None and (not isinstance(value, str) or len(value) > 4096)
                for value in self.repository_identity.values()
            )
        ):
            raise ValueError("Invalid checkpoint repository identity")
        if not isinstance(self.created_at, str) or len(self.created_at) > 64:
            raise ValueError("Invalid checkpoint timestamp")
        try:
            if datetime.fromisoformat(self.created_at).tzinfo is None:
                raise ValueError("Invalid checkpoint timestamp")
        except ValueError as exc:
            raise ValueError("Invalid checkpoint timestamp") from exc
        if (
            not isinstance(self.files, list) or not 1 <= len(self.files) <= _MAX_PATHS
            or len({item.path for item in self.files}) != len(self.files)
        ):
            raise ValueError("Invalid checkpoint file list")
        for item in self.files:
            item.validate()
        actual_size = sum(item.size_bytes for item in self.files)
        if (
            type(self.total_size_bytes) is not int
            or self.total_size_bytes != actual_size or actual_size > _MAX_TOTAL_BYTES
        ):
            raise ValueError("Invalid checkpoint size")
        if type(self.complete) is not bool or self.complete != all(item.complete for item in self.files):
            raise ValueError("Invalid checkpoint completeness")
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        if self.integrity_sha256 != expected:
            raise ValueError("Checkpoint metadata integrity failure")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {**self.payload(), "integrity_sha256": self.integrity_sha256}

    @classmethod
    def from_dict(cls, value: object) -> CheckpointRecord:
        keys = {
            "checkpoint_id", "owner_id", "task_id", "workspace_identity",
            "backend_identity", "repository_identity", "created_at", "files",
            "total_size_bytes", "complete", "integrity_sha256",
        }
        if not isinstance(value, dict) or set(value) != keys or not isinstance(value["files"], list):
            raise ValueError("Invalid checkpoint metadata fields")
        data = dict(value)
        data["files"] = [CheckpointFile.from_dict(item) for item in data["files"]]
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True)
class RestoreFile:
    path: str
    original_exists: bool
    original_content: str | None
    expected_exists: bool
    expected_sha256: str | None
    restore_sha256: str | None


@dataclass(frozen=True)
class PreparedRestore:
    checkpoint_id: str
    files: tuple[RestoreFile, ...]
    limitation: str


class CheckpointManager:
    """Private bounded snapshots and conflict-checked restoration preparation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = ConversationStorage(settings.history_dir)
        self.directory = checked_path(self.storage.root / "checkpoints")
        if self.directory.exists():
            self._validate_directory()
        self.snapshots: SnapshotStore | None = None
        self._lock = threading.RLock()

    def create(
        self,
        session: Session,
        repository: RepositoryIndex,
        paths: list[str],
        *,
        task_id: str | None = None,
        repository_identity: dict[str, str | None] | None = None,
        require_complete: bool = True,
        cancellation: threading.Event | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(session, Session) or not isinstance(repository, RepositoryIndex)
            or not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_PATHS
            or len(set(paths)) != len(paths) or any(not _safe_path(path) for path in paths)
            or type(require_complete) is not bool
            or task_id is not None and (
                not isinstance(task_id, str) or not task_id or len(task_id) > 128
            )
        ):
            return _failure("CHECKPOINT_SCOPE_VIOLATION", "Checkpoint request is invalid.")
        root = self._validated_workspace(session, repository)
        self._ensure_private_storage_outside(root)
        if cancellation is not None and cancellation.is_set():
            return _failure("CANCELLED", "Checkpoint capture was cancelled.")
        self._initialize_private_storage()
        start = time.monotonic()
        files: list[CheckpointFile] = []
        total = 0
        incomplete: list[str] = []
        for path in paths:
            if cancellation is not None and cancellation.is_set():
                return _failure("CANCELLED", "Checkpoint capture was cancelled.")
            if time.monotonic() - start > _MAX_CAPTURE_SECONDS:
                incomplete.append(f"{path}: checkpoint capture time limit reached")
                files.append(self._incomplete(path, incomplete[-1]))
                continue
            try:
                first = repository.read_file_bytes(path, max_bytes=1_048_576)
                second = repository.read_file_bytes(path, max_bytes=1_048_576)
                if first != second:
                    raise OSError("File changed during snapshot capture")
                digest = hashlib.sha256(first).hexdigest() if first is not None else None
                size = len(first) if first is not None else 0
                if total + size > _MAX_TOTAL_BYTES:
                    raise OSError("Checkpoint total-size limit reached")
                assert self.snapshots is not None
                reference = self.snapshots.save(first) if first is not None else None
                total += size
                files.append(CheckpointFile(
                    path=path,
                    exists=first is not None,
                    sha256=digest,
                    snapshot_ref=reference,
                    size_bytes=size,
                    expected_exists=first is not None,
                    expected_sha256=digest,
                    complete=True,
                ))
            except (OSError, ValueError, InterruptedError) as exc:
                incomplete.append(f"{path}: {str(exc)[:256]}")
                files.append(self._incomplete(path, incomplete[-1]))
        complete = not incomplete and len(files) == len(paths)
        if cancellation is not None and cancellation.is_set():
            return _failure("CANCELLED", "Checkpoint capture was cancelled.")
        if require_complete and not complete:
            return _failure(
                "CHECKPOINT_INCOMPLETE",
                "A complete checkpoint was required but one or more files could not be captured.",
                records=[{"path": item.path, "complete": item.complete, "limitation": item.limitation}
                         for item in files],
            )
        record = CheckpointRecord(
            checkpoint_id=uuid4().hex,
            owner_id=session.session_id,
            task_id=task_id,
            workspace_identity=str(root),
            backend_identity=self.settings.execution_mode,
            repository_identity=repository_identity,
            created_at=_now(),
            files=files,
            total_size_bytes=total,
            complete=complete,
            integrity_sha256="",
        )
        record.seal()
        try:
            with self._lock:
                if self._record_count() >= _MAX_CHECKPOINTS:
                    raise OSError("Checkpoint count limit reached")
                serialized = record.to_dict()
                if len(_canonical(serialized)) > _MAX_METADATA_BYTES:
                    raise OSError("Checkpoint metadata size limit reached")
                write_private_json(self._record_path(record.checkpoint_id), serialized)
        except (OSError, ValueError) as exc:
            return _failure("RESOURCE_LIMIT", str(exc)[:512])
        return {
            "ok": True,
            "success": True,
            "operation": "git_checkpoint",
            "checkpoint_id": record.checkpoint_id,
            "complete": record.complete,
            "task_id": record.task_id,
            "workspace_identity": record.workspace_identity,
            "repository_identity": record.repository_identity,
            "included_paths": [item.path for item in files],
            "total_size_bytes": total,
            "integrity_sha256": record.integrity_sha256,
            "limitations": [
                *incomplete,
                *(
                    ["Repository identity unavailable; checkpoint is bound to the validated workspace."]
                    if repository_identity is None else []
                ),
            ],
        }

    def prepare_restore(
        self,
        session: Session,
        repository: RepositoryIndex,
        checkpoint_id: str,
        paths: list[str],
    ) -> PreparedRestore | dict[str, Any]:
        if (
            not isinstance(session, Session) or not isinstance(repository, RepositoryIndex)
            or not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id)
            or not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_PATHS
            or len(set(paths)) != len(paths) or any(not _safe_path(path) for path in paths)
        ):
            return _failure(
                "CHECKPOINT_SCOPE_VIOLATION",
                "Restore request is invalid.",
                operation="restore_checkpoint",
            )
        root = self._validated_workspace(session, repository)
        self._ensure_private_storage_outside(root)
        self._initialize_private_storage()
        try:
            record = self.load(checkpoint_id)
        except FileNotFoundError:
            return _failure(
                "CHECKPOINT_NOT_FOUND", "Checkpoint does not exist.",
                operation="restore_checkpoint",
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return _failure(
                "CHECKPOINT_INTEGRITY_FAILURE", str(exc)[:512],
                operation="restore_checkpoint",
            )
        if (
            record.owner_id != session.session_id
            or record.workspace_identity != str(root)
            or record.backend_identity != self.settings.execution_mode
        ):
            return _failure(
                "CHECKPOINT_SCOPE_VIOLATION",
                "Checkpoint owner, workspace, or execution backend does not match.",
                operation="restore_checkpoint",
            )
        entries = {item.path: item for item in record.files}
        if any(path not in entries for path in paths):
            return _failure(
                "CHECKPOINT_SCOPE_VIOLATION",
                "Restore selected a path outside the checkpoint.",
                operation="restore_checkpoint",
            )
        prepared: list[RestoreFile] = []
        conflicts: list[dict[str, Any]] = []
        for path in paths:
            item = entries[path]
            if not item.complete:
                return _failure(
                    "CHECKPOINT_INCOMPLETE",
                    f"Checkpoint content is incomplete for {path}.",
                    records=[{"path": path, "outcome": "blocked", "limitation": item.limitation}],
                    operation="restore_checkpoint",
                )
            try:
                current_first = repository.read_file_bytes(path, max_bytes=1_048_576)
                current_second = repository.read_file_bytes(path, max_bytes=1_048_576)
                if current_first != current_second:
                    raise OSError("File changed during restore preflight")
                current_hash = hashlib.sha256(current_first).hexdigest() if current_first is not None else None
                if (
                    (current_first is not None) != item.expected_exists
                    or current_hash != item.expected_sha256
                ):
                    conflicts.append({
                        "path": path,
                        "expected_hash": item.expected_sha256,
                        "current_hash": current_hash,
                        "error_code": "EXTERNAL_MODIFICATION_DETECTED",
                    })
                    continue
                assert self.snapshots is not None
                original = self.snapshots.load(item.snapshot_ref) if item.snapshot_ref else None
                if original is not None and hashlib.sha256(original).hexdigest() != item.sha256:
                    raise OSError("Snapshot hash does not match checkpoint metadata")
                original_text = original.decode("utf-8") if original is not None else None
                prepared.append(RestoreFile(
                    path=path,
                    original_exists=item.exists,
                    original_content=original_text,
                    expected_exists=item.expected_exists,
                    expected_sha256=item.expected_sha256,
                    restore_sha256=item.sha256,
                ))
            except UnicodeDecodeError:
                return _failure(
                    "CHECKPOINT_INCOMPLETE",
                    f"Binary or non-UTF-8 checkpoint content cannot be restored through the existing text mutation tools: {path}.",
                    records=[{"path": path, "outcome": "blocked"}],
                    operation="restore_checkpoint",
                )
            except (OSError, ValueError, InterruptedError) as exc:
                return _failure(
                    "CHECKPOINT_INTEGRITY_FAILURE",
                    f"Restore preflight failed for {path}: {str(exc)[:256]}",
                    operation="restore_checkpoint",
                )
        if conflicts:
            return _failure(
                "CHECKPOINT_CONFLICT",
                "One or more files changed since the checkpoint's last confirmed task state.",
                records=conflicts,
                operation="restore_checkpoint",
            )
        return PreparedRestore(
            checkpoint_id,
            tuple(prepared),
            "Multi-file restore is preflighted as a batch but is not atomic; per-file outcomes are reported.",
        )

    def mark_restored(
        self,
        checkpoint_id: str,
        path: str,
    ) -> None:
        record = self.load(checkpoint_id)
        for item in record.files:
            if item.path == path:
                item.expected_exists = item.exists
                item.expected_sha256 = item.sha256
                record.seal()
                write_private_json(self._record_path(checkpoint_id), record.to_dict())
                return
        raise ValueError("Restored path is not part of the checkpoint")

    def update_task_postimage(
        self,
        task_id: str,
        workspace_identity: str,
        path: str,
        exists: bool,
        digest: str | None,
    ) -> None:
        if not isinstance(task_id, str) or not task_id or not _safe_path(path):
            raise ValueError("Invalid checkpoint task-postimage update")
        if type(exists) is not bool or exists != (digest is not None):
            raise ValueError("Invalid checkpoint expected-current state")
        if digest is not None and not _HASH.fullmatch(digest):
            raise ValueError("Invalid checkpoint postimage digest")
        if not self.directory.exists():
            return
        with self._lock:
            for entry in os.scandir(self.directory):
                if not entry.name.endswith(".json") or not _ID.fullmatch(entry.name[:-5]):
                    continue
                record = self.load(entry.name[:-5])
                if record.task_id != task_id or record.workspace_identity != workspace_identity:
                    continue
                for item in record.files:
                    if item.path == path and item.complete:
                        item.expected_exists = exists
                        item.expected_sha256 = digest
                        record.seal()
                        write_private_json(self._record_path(record.checkpoint_id), record.to_dict())

    def cleanup(self, *, older_than_seconds: int) -> int:
        if type(older_than_seconds) is not int or older_than_seconds < 0:
            raise ValueError("Checkpoint cleanup age must be a nonnegative integer")
        if not self.directory.exists():
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
        removed = 0
        with self._lock:
            self._validate_directory()
            for entry in os.scandir(self.directory):
                if not entry.name.endswith(".json") or not _ID.fullmatch(entry.name[:-5]):
                    raise OSError("Unexpected checkpoint metadata entry")
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Checkpoint metadata is not a regular file")
                if info.st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            self._prune_unreferenced_snapshots()
        return removed

    def load(self, checkpoint_id: str) -> CheckpointRecord:
        path = self._record_path(checkpoint_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise FileNotFoundError("Checkpoint not found") from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _MAX_METADATA_BYTES
            ):
                raise OSError("Checkpoint metadata permissions or type are invalid")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(_MAX_METADATA_BYTES + 1)
                after = os.fstat(handle.fileno())
            if (
                len(raw) > _MAX_METADATA_BYTES
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise OSError("Checkpoint metadata changed while being read")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        value = json.loads(raw)
        return CheckpointRecord.from_dict(value)

    def _validated_workspace(self, session: Session, repository: RepositoryIndex) -> Path:
        root = validate_workspace(
            Path(session.workspace),
            self.settings,
            sandbox=self.settings.execution_mode == "sandbox",
        )
        if repository.root != root:
            raise ValueError("Checkpoint workspace identity changed")
        return root

    def _ensure_private_storage_outside(self, workspace: Path) -> None:
        if (
            self.directory.is_relative_to(workspace)
            or checked_path(self.storage.root / "checkpoint-snapshots").is_relative_to(workspace)
            or checked_path(self.storage.root / "task-snapshots").is_relative_to(workspace)
        ):
            raise ValueError("Private checkpoint and snapshot storage must be outside the indexed workspace")

    def _initialize_private_storage(self) -> None:
        self.storage.initialize()
        if self.directory.exists():
            self._validate_directory()
        else:
            self.directory.mkdir(mode=0o700)
        self.snapshots = SnapshotStore(
            self.settings,
            directory_name="checkpoint-snapshots",
        )

    def _prune_unreferenced_snapshots(self) -> None:
        if self.snapshots is None:
            self.snapshots = SnapshotStore(
                self.settings,
                directory_name="checkpoint-snapshots",
            )
        referenced: set[str] = set()
        for entry in os.scandir(self.directory):
            if not entry.name.endswith(".json") or not _ID.fullmatch(entry.name[:-5]):
                raise OSError("Unexpected checkpoint metadata entry")
            record = self.load(entry.name[:-5])
            referenced.update(
                item.snapshot_ref for item in record.files
                if item.snapshot_ref is not None
            )
        self.snapshots._validate_directory()
        for entry in os.scandir(self.snapshots.directory):
            if not _HASH.fullmatch(entry.name):
                raise OSError("Unexpected checkpoint snapshot entry")
            info = entry.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise OSError("Checkpoint snapshot permissions or type are invalid")
            if entry.name not in referenced:
                os.unlink(entry.path)

    def _record_count(self) -> int:
        count = 0
        for entry in os.scandir(self.directory):
            if not entry.name.endswith(".json") or not _ID.fullmatch(entry.name[:-5]):
                raise OSError("Unexpected checkpoint metadata entry")
            count += 1
        return count

    def _record_path(self, checkpoint_id: str) -> Path:
        if not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id):
            raise ValueError("Invalid checkpoint identity")
        self._validate_directory()
        return checked_path(self.directory / f"{checkpoint_id}.json")

    def _validate_directory(self) -> None:
        if self.directory.is_symlink():
            raise ValueError("Checkpoint directory cannot be a symlink")
        info = self.directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Checkpoint directory must be private and user-owned")

    @staticmethod
    def _incomplete(path: str, limitation: str) -> CheckpointFile:
        return CheckpointFile(
            path, False, None, None, 0, False, None, False, limitation[:512],
        )


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _failure(
    error_code: str,
    error: str,
    *,
    records: list[dict[str, Any]] | None = None,
    operation: str = "git_checkpoint",
) -> dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "operation": operation,
        "error_code": error_code,
        "error": error[:1024],
        "records": records or [],
        "result_count": len(records or []),
        "truncated": False,
        "limitations": [],
    }
