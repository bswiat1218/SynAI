from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from synai.config import Settings
from synai.intelligence import RepositoryIndex
from synai.storage import ConversationStorage, checked_path


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILE_BYTES = 1_048_576
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 1024
_MAX_DIFF_CHARACTERS = 4096


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute() and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


@dataclass
class ChangeBaseline:
    task_id: str
    workspace_identity: str
    path: str
    exists: bool
    file_type: str
    sha256: str | None
    snapshot_ref: str | None
    byte_size: int
    capture_sequence: int
    captured_at: str
    execution_id: str
    complete: bool
    limitation: str | None = None

    def validate(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id or len(self.task_id) > 128:
            raise ValueError("Invalid change baseline task ID")
        if (
            not isinstance(self.workspace_identity, str)
            or not self.workspace_identity or len(self.workspace_identity) > 4096
        ):
            raise ValueError("Invalid change baseline workspace identity")
        if not _safe_relative(self.path):
            raise ValueError("Invalid change baseline path")
        if type(self.exists) is not bool or self.file_type not in {"regular", "missing"}:
            raise ValueError("Invalid change baseline file state")
        if self.exists != (self.file_type == "regular"):
            raise ValueError("Change baseline existence and file type disagree")
        if self.sha256 is not None and not _DIGEST.fullmatch(self.sha256):
            raise ValueError("Invalid change baseline digest")
        if self.exists and (self.sha256 is None or self.snapshot_ref != self.sha256):
            raise ValueError("Existing change baseline requires a private content reference")
        if not self.exists and (self.sha256 is not None or self.snapshot_ref is not None):
            raise ValueError("Missing change baseline cannot reference content")
        if type(self.byte_size) is not int or not 0 <= self.byte_size <= _MAX_FILE_BYTES:
            raise ValueError("Invalid change baseline size")
        if type(self.capture_sequence) is not int or self.capture_sequence < 1:
            raise ValueError("Invalid change baseline capture sequence")
        if not isinstance(self.captured_at, str) or len(self.captured_at) > 64:
            raise ValueError("Invalid change baseline timestamp")
        try:
            parsed = datetime.fromisoformat(self.captured_at)
        except ValueError as exc:
            raise ValueError("Invalid change baseline timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("Invalid change baseline timestamp")
        if not isinstance(self.execution_id, str) or not self.execution_id or len(self.execution_id) > 128:
            raise ValueError("Invalid change baseline execution ID")
        if type(self.complete) is not bool:
            raise ValueError("Invalid change baseline completeness")
        if self.limitation is not None and (
            not isinstance(self.limitation, str) or len(self.limitation) > 512
        ):
            raise ValueError("Invalid change baseline limitation")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_id": self.task_id,
            "workspace_identity": self.workspace_identity,
            "path": self.path,
            "exists": self.exists,
            "file_type": self.file_type,
            "sha256": self.sha256,
            "snapshot_ref": self.snapshot_ref,
            "byte_size": self.byte_size,
            "capture_sequence": self.capture_sequence,
            "captured_at": self.captured_at,
            "execution_id": self.execution_id,
            "complete": self.complete,
            "limitation": self.limitation,
        }

    @classmethod
    def from_dict(cls, value: object) -> ChangeBaseline:
        keys = {
            "task_id", "workspace_identity", "path", "exists", "file_type",
            "sha256", "snapshot_ref", "byte_size", "capture_sequence",
            "captured_at", "execution_id", "complete", "limitation",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid change baseline fields")
        result = cls(**value)
        result.validate()
        return result


@dataclass
class MutationEvidence:
    task_id: str
    execution_id: str
    repair_attempt_id: int | None
    path: str
    operation: str
    before_hash: str | None
    after_hash: str | None
    before_exists: bool
    after_exists: bool | None
    outcome: str
    diff: str | None
    truncated: bool
    uncertain: bool
    capture_provenance: str
    before_snapshot_ref: str | None = None
    after_snapshot_ref: str | None = None
    limitations: tuple[str, ...] = ()
    captured_at: str = field(default_factory=_timestamp)

    def validate(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id or len(self.task_id) > 128:
            raise ValueError("Invalid mutation evidence task ID")
        if not isinstance(self.execution_id, str) or not self.execution_id or len(self.execution_id) > 128:
            raise ValueError("Invalid mutation evidence execution ID")
        if self.repair_attempt_id is not None and (
            type(self.repair_attempt_id) is not int or self.repair_attempt_id < 1
        ):
            raise ValueError("Invalid mutation evidence repair attempt ID")
        if not _safe_relative(self.path):
            raise ValueError("Invalid mutation evidence path")
        if self.operation not in {"create", "modify", "delete", "write", "patch"}:
            raise ValueError("Invalid mutation evidence operation")
        for digest in (self.before_hash, self.after_hash, self.before_snapshot_ref, self.after_snapshot_ref):
            if digest is not None and not _DIGEST.fullmatch(digest):
                raise ValueError("Invalid mutation evidence digest")
        if type(self.before_exists) is not bool or (
            self.after_exists is not None and type(self.after_exists) is not bool
        ):
            raise ValueError("Invalid mutation evidence file state")
        if self.outcome not in {"succeeded", "no_op", "failed", "interrupted", "uncertain"}:
            raise ValueError("Invalid mutation evidence outcome")
        if self.diff is not None and (
            not isinstance(self.diff, str) or len(self.diff) > _MAX_DIFF_CHARACTERS
        ):
            raise ValueError("Invalid mutation evidence diff")
        if type(self.truncated) is not bool or type(self.uncertain) is not bool:
            raise ValueError("Invalid mutation evidence flags")
        if not isinstance(self.capture_provenance, str) or not self.capture_provenance or len(self.capture_provenance) > 128:
            raise ValueError("Invalid mutation evidence provenance")
        if (
            not isinstance(self.limitations, tuple) or len(self.limitations) > 8
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in self.limitations)
        ):
            raise ValueError("Invalid mutation evidence limitations")
        try:
            parsed = datetime.fromisoformat(self.captured_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid mutation evidence timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("Invalid mutation evidence timestamp")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        value = dict(vars(self))
        value["limitations"] = list(self.limitations)
        return value

    @classmethod
    def from_dict(cls, value: object) -> MutationEvidence:
        keys = {
            "task_id", "execution_id", "repair_attempt_id", "path", "operation",
            "before_hash", "after_hash", "before_exists", "after_exists",
            "outcome", "diff", "truncated", "uncertain", "capture_provenance",
            "before_snapshot_ref", "after_snapshot_ref", "limitations", "captured_at",
        }
        if (
            not isinstance(value, dict) or set(value) != keys
            or not isinstance(value["limitations"], list)
        ):
            raise ValueError("Invalid mutation evidence fields")
        result = cls(**{**value, "limitations": tuple(value["limitations"])})
        result.validate()
        return result


class SnapshotStore:
    """Private content-addressed storage separate from conversation/history JSON."""

    def __init__(
        self,
        settings: Settings,
        *,
        directory_name: str = "task-snapshots",
    ) -> None:
        if directory_name not in {"task-snapshots", "checkpoint-snapshots"}:
            raise ValueError("Unsupported private snapshot store")
        storage = ConversationStorage(settings.history_dir)
        storage.initialize()
        self.directory = checked_path(storage.root / directory_name)
        if self.directory.exists():
            self._validate_directory()
        else:
            self.directory.mkdir(mode=0o700)
        self._lock = threading.RLock()

    def save(self, content: bytes) -> str:
        if not isinstance(content, bytes) or len(content) > _MAX_FILE_BYTES:
            raise ValueError("Snapshot content exceeds the configured per-file bound")
        digest = hashlib.sha256(content).hexdigest()
        target = checked_path(self.directory / digest)
        with self._lock:
            self._validate_directory()
            if target.exists():
                existing = self.load(digest)
                if existing != content:
                    raise OSError("Content-addressed snapshot integrity failure")
                return digest
            files, size = self._inventory()
            if files >= _MAX_SNAPSHOT_FILES or size + len(content) > _MAX_SNAPSHOT_BYTES:
                raise OSError("Snapshot retention resource limit reached")
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        return digest

    def load(self, reference: str) -> bytes:
        if not isinstance(reference, str) or not _DIGEST.fullmatch(reference):
            raise ValueError("Invalid private snapshot reference")
        target = checked_path(self.directory / reference)
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _MAX_FILE_BYTES
            ):
                raise OSError("Private snapshot permissions or type are invalid")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                content = handle.read(_MAX_FILE_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) > _MAX_FILE_BYTES or hashlib.sha256(content).hexdigest() != reference:
            raise OSError("Private snapshot integrity failure")
        return content

    def cleanup(self, *, older_than_seconds: int) -> int:
        if type(older_than_seconds) is not int or older_than_seconds < 0:
            raise ValueError("Snapshot cleanup age must be a nonnegative integer")
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
        removed = 0
        with self._lock:
            self._validate_directory()
            for entry in os.scandir(self.directory):
                if not _DIGEST.fullmatch(entry.name):
                    raise OSError("Unexpected private snapshot entry")
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Private snapshot entry is not a regular file")
                if info.st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
        return removed

    def _validate_directory(self) -> None:
        if self.directory.is_symlink():
            raise ValueError("Private snapshot directory cannot be a symlink")
        info = self.directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Private snapshot directory must be user-owned and mode 0700")

    def _inventory(self) -> tuple[int, int]:
        files = 0
        total = 0
        for entry in os.scandir(self.directory):
            if not _DIGEST.fullmatch(entry.name):
                raise OSError("Unexpected private snapshot entry")
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError("Private snapshot entry has unsafe permissions or type")
            files += 1
            total += info.st_size
        return files, total


class BaselineCaptureError(OSError):
    pass


class TaskChangeTracker:
    def __init__(
        self,
        task: Any,
        repository: RepositoryIndex,
        settings: Settings,
        workspace_identity: str,
    ) -> None:
        self.task = task
        self.repository = repository
        self.workspace_identity = workspace_identity
        workspace = Path(workspace_identity).resolve()
        snapshot_directory = checked_path(
            settings.history_dir.expanduser() / "task-snapshots",
        )
        if snapshot_directory.is_relative_to(workspace):
            raise BaselineCaptureError(
                "Private task snapshots must be stored outside the indexed workspace",
            )
        self.store = SnapshotStore(settings)
        self._baselines = {item.path: item for item in task.change_baselines}
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def before_mutation(
        self,
        execution_id: str,
        path: str,
        operation: str,
        *,
        repair_attempt_id: int | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if not _safe_relative(path):
            raise BaselineCaptureError("Mutation baseline path is unsafe")
        with self._lock:
            if execution_id in self._pending:
                raise BaselineCaptureError("Mutation execution already has a preimage")
            try:
                before = self._stable_read(path)
            except (OSError, ValueError, InterruptedError) as exc:
                raise BaselineCaptureError(
                    f"CHANGE_BASELINE_UNAVAILABLE: {path}: {str(exc)[:256]}",
                ) from exc
            content, digest = before
            expected_after = self._expected_postimage(
                tool_name, arguments, content,
            )
            baseline = self._baselines.get(path)
            if baseline is None:
                reference = self.store.save(content) if content is not None else None
                baseline = ChangeBaseline(
                    task_id=self.task.task_id,
                    workspace_identity=self.workspace_identity,
                    path=path,
                    exists=content is not None,
                    file_type="regular" if content is not None else "missing",
                    sha256=digest,
                    snapshot_ref=reference,
                    byte_size=len(content) if content is not None else 0,
                    capture_sequence=len(self.task.change_baselines) + 1,
                    captured_at=_timestamp(),
                    execution_id=execution_id,
                    complete=True,
                )
                baseline.validate()
                self.task.change_baselines.append(baseline)
                self._baselines[path] = baseline
            previous = next(
                (
                    item for item in reversed(self.task.change_evidence)
                    if item.path == path and item.outcome in {"succeeded", "no_op"}
                ),
                None,
            )
            continuity_lost = (
                previous.after_hash != digest
                if previous is not None
                else baseline.sha256 != digest
                or baseline.exists != (content is not None)
            )
            self._pending[execution_id] = {
                "path": path,
                "operation": operation,
                "content": content,
                "digest": digest,
                "reference": self.store.save(content) if content is not None else None,
                "repair_attempt_id": repair_attempt_id,
                "continuity_lost": continuity_lost,
                "expected_after_exists": expected_after is not None,
                "expected_after_hash": (
                    hashlib.sha256(expected_after).hexdigest()
                    if expected_after is not None else None
                ),
            }

    def after_mutation(
        self,
        execution_id: str,
        result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.pop(execution_id, None)
            if pending is None:
                raise OSError("Mutation preimage is unavailable")
            path = pending["path"]
            limitations: list[str] = []
            try:
                after_content, after_hash = self._stable_read(path)
                after_exists = after_content is not None
                after_ref = self.store.save(after_content) if after_content is not None else None
            except (OSError, ValueError, InterruptedError) as exc:
                after_content, after_hash, after_exists, after_ref = None, None, None, None
                limitations.append(f"Postimage unavailable: {str(exc)[:256]}")
            backend_ok = isinstance(result, dict) and result.get("ok") is True
            changed = pending["digest"] != after_hash
            expected_after_matches = (
                after_exists == pending["expected_after_exists"]
                and after_hash == pending["expected_after_hash"]
            )
            continuity_lost = pending["continuity_lost"]
            uncertain = (
                after_exists is None or continuity_lost or result is None
                or not backend_ok and changed
                or backend_ok and not expected_after_matches
            )
            if result is None:
                outcome = "interrupted"
                limitations.append("Backend outcome is uncertain; operation was not replayed.")
            elif not backend_ok and changed:
                outcome = "uncertain"
                limitations.append("Workspace changed despite an unsuccessful backend result.")
            elif not backend_ok:
                outcome = "failed"
            elif not expected_after_matches:
                outcome = "uncertain"
                limitations.append(
                    "Observed postimage differed from the requested mutation result; "
                    "external or concurrent changes cannot be attributed."
                )
            elif not changed:
                outcome = "no_op"
            elif continuity_lost:
                outcome = "uncertain"
                limitations.append("An external change broke continuity with the prior mutation.")
            else:
                outcome = "succeeded"
            if continuity_lost:
                limitations.append("Preimage did not match the prior confirmed postimage.")
            diff, diff_truncated, diff_limitation = bounded_change_diff(
                pending["content"], after_content, path,
            )
            if diff_limitation:
                limitations.append(diff_limitation)
            evidence = MutationEvidence(
                task_id=self.task.task_id,
                execution_id=execution_id,
                repair_attempt_id=pending["repair_attempt_id"],
                path=path,
                operation=pending["operation"],
                before_hash=pending["digest"],
                after_hash=after_hash,
                before_exists=pending["content"] is not None,
                after_exists=after_exists,
                outcome=outcome,
                diff=diff,
                truncated=diff_truncated,
                uncertain=uncertain,
                capture_provenance="RepositoryIndex.read_file_bytes",
                before_snapshot_ref=pending["reference"],
                after_snapshot_ref=after_ref,
                limitations=tuple(dict.fromkeys(limitations))[:8],
            )
            evidence.validate()
            self.task.change_evidence.append(evidence)
            if len(self.task.change_evidence) > 512:
                self.task.change_evidence.pop(0)
            return {
                "mutation_changed": outcome == "succeeded",
                "change_attribution_complete": outcome in {"succeeded", "no_op", "failed"},
                "change_evidence": evidence.to_dict(),
            }

    @staticmethod
    def _expected_postimage(
        tool_name: str,
        arguments: dict[str, Any],
        before: bytes | None,
    ) -> bytes | None:
        if tool_name == "write_file":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ValueError("Write mutation content is unavailable")
            return content.encode("utf-8")
        if tool_name == "delete_file":
            return None
        if tool_name == "patch_file":
            if before is None:
                raise ValueError("Patch preimage is missing")
            try:
                current = before.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Patch preimage is not valid UTF-8") from exc
            old, new = arguments.get("old"), arguments.get("new")
            if (
                not isinstance(old, str) or not old
                or not isinstance(new, str) or current.count(old) != 1
            ):
                raise ValueError("Patch result cannot be determined exactly")
            return current.replace(old, new, 1).encode("utf-8")
        raise ValueError("Mutation tool is unsupported for change attribution")

    def _stable_read(self, path: str) -> tuple[bytes | None, str | None]:
        first = self.repository.read_file_bytes(path, max_bytes=_MAX_FILE_BYTES)
        second = self.repository.read_file_bytes(path, max_bytes=_MAX_FILE_BYTES)
        if first != second:
            raise OSError("File changed between secure preimage reads")
        return first, hashlib.sha256(first).hexdigest() if first is not None else None


def bounded_change_diff(
    before: bytes | None,
    after: bytes | None,
    path: str,
) -> tuple[str | None, bool, str | None]:
    if before == after:
        return None, False, None
    if any(content is not None and b"\0" in content for content in (before, after)):
        return None, False, "Binary content changed; only hashes and existence states are recorded."
    try:
        before_text = before.decode("utf-8") if before is not None else ""
        after_text = after.decode("utf-8") if after is not None else ""
    except UnicodeDecodeError:
        return None, False, "Non-UTF-8 content changed; only hashes and existence states are recorded."
    lines = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"a/{path}" if before is not None else "/dev/null",
        tofile=f"b/{path}" if after is not None else "/dev/null",
    )
    value = "".join(lines)
    if len(value) > _MAX_DIFF_CHARACTERS:
        return value[:_MAX_DIFF_CHARACTERS], True, "Mutation diff was truncated."
    return value or None, False, None
