from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from synai.intelligence import RepositoryIndex
from synai.web.database import MetadataDatabase
from synai.web.projects import WorkspaceIdentity


class WorkspaceLeaseError(RuntimeError):
    pass


class LeaseRecoveryRequired(WorkspaceLeaseError):
    pass


class WorkspaceContention(WorkspaceLeaseError):
    pass


@dataclass(frozen=True)
class InterruptedLease:
    workspace_identity: WorkspaceIdentity
    owner: WorkspaceOwner
    generation: int


@dataclass(frozen=True)
class RecoveryEvidence:
    workspace_identity: str
    owner_id: str
    workflow_id: str
    task_id: str
    generation: int
    process_tree_reaped: bool
    evidence_id: str
    confirmed_at: int


class InterruptedOwnerRecovery(Protocol):
    async def confirm_stopped(self, lease: InterruptedLease) -> RecoveryEvidence | None: ...


@dataclass(frozen=True)
class WorkspaceOwner:
    owner_id: str
    workflow_id: str
    task_id: str
    label: str

    def validate(self) -> None:
        for value, maximum in (
            (self.owner_id, 128), (self.workflow_id, 128),
            (self.task_id, 128), (self.label, 128),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValueError("Workspace lease owner metadata is invalid")
        if any("\x00" in value for value in (
            self.owner_id, self.workflow_id, self.task_id, self.label,
        )):
            raise ValueError("Workspace lease owner metadata is invalid")


@dataclass(frozen=True)
class FilePreimage:
    workspace_identity: str
    path: str
    existed: bool
    device_id: int | None
    inode: int | None
    size_bytes: int | None
    sha256: str | None


@dataclass
class _HeldLease:
    identity: WorkspaceIdentity
    owner: WorkspaceOwner
    generation: int
    fencing_token: str
    descriptor: int
    async_lock: asyncio.Lock
    references: int = 1


class WorkspaceLease:
    def __init__(
        self,
        coordinator: WorkspaceCoordinator,
        held: _HeldLease,
    ) -> None:
        self._coordinator = coordinator
        self._held = held
        self._released = False

    @property
    def workspace_identity(self) -> str:
        return self._held.identity.key

    @property
    def owner(self) -> WorkspaceOwner:
        return self._held.owner

    @property
    def generation(self) -> int:
        return self._held.generation

    @property
    def fencing_token(self) -> str:
        return self._held.fencing_token

    async def release(self) -> None:
        if self._released:
            raise WorkspaceLeaseError("Workspace lease was already released")
        self._released = True
        await self._coordinator._release(self._held)

    async def __aenter__(self) -> WorkspaceLease:
        if self._released:
            raise WorkspaceLeaseError("Workspace lease is no longer active")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.release()


class WorkspaceCoordinator:
    """Cross-process exclusive leases with an authoritative generation token."""

    MAX_ACQUIRE_SECONDS = 300

    def __init__(self, database: MetadataDatabase, *, poll_seconds: float = 0.05) -> None:
        if not 0 < poll_seconds <= 1:
            raise ValueError("Workspace-lock poll interval must be bounded")
        self.database = database
        self.directory = database.directory / "workspace-locks"
        self.poll_seconds = poll_seconds
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._held: dict[str, _HeldLease] = {}

    async def acquire(
        self,
        identity: WorkspaceIdentity,
        owner: WorkspaceOwner,
        *,
        timeout: float = 5,
        recover_interrupted: bool = False,
        recovery_authority: InterruptedOwnerRecovery | None = None,
    ) -> WorkspaceLease:
        owner.validate()
        if (
            not isinstance(identity, WorkspaceIdentity)
            or type(recover_interrupted) is not bool
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= self.MAX_ACQUIRE_SECONDS
        ):
            raise ValueError("Workspace lease request is invalid")
        _validate_current_identity(identity)
        key = identity.key
        active = self._held.get(key)
        if active is not None:
            if (
                active.owner.owner_id != owner.owner_id
                or active.owner.workflow_id != owner.workflow_id
                or active.owner.task_id != owner.task_id
            ):
                pass
            else:
                active.references += 1
                return WorkspaceLease(self, active)
        async_lock = self._async_locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(async_lock.acquire(), timeout=float(timeout))
        except TimeoutError as exc:
            raise WorkspaceContention("Workspace lease acquisition timed out") from exc
        deadline = time.monotonic() + float(timeout)
        descriptor: int | None = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _validate_private_directory(self.directory)
            lock_path = self.directory / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            _validate_private_file(descriptor)
            while True:
                _validate_current_identity(identity)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WorkspaceContention("Workspace lease acquisition timed out")
                    await asyncio.sleep(min(self.poll_seconds, max(0, deadline - time.monotonic())))
            recovery_evidence = None
            if recover_interrupted:
                previous = self._latest_active(identity.key)
                if previous is not None:
                    if recovery_authority is None:
                        raise LeaseRecoveryRequired(
                            "Interrupted-owner recovery requires a trusted runner reaping authority",
                        )
                    request = InterruptedLease(
                        identity,
                        WorkspaceOwner(
                            previous["owner_id"],
                            previous["workflow_id"],
                            previous["task_id"],
                            previous["label"],
                        ),
                        previous["generation"],
                    )
                    try:
                        recovery_evidence = await asyncio.wait_for(
                            recovery_authority.confirm_stopped(request),
                            timeout=max(0.001, deadline - time.monotonic()),
                        )
                    except TimeoutError as exc:
                        raise WorkspaceContention(
                            "Interrupted-owner recovery timed out",
                        ) from exc
                    if not _valid_recovery_evidence(request, recovery_evidence):
                        raise LeaseRecoveryRequired(
                            "The prior runner did not confirm that all workspace writers stopped",
                        )
            generation, token = self._start_generation(
                identity, owner, recovery_evidence,
            )
            held = _HeldLease(identity, owner, generation, token, descriptor, async_lock)
            self._held[key] = held
            return WorkspaceLease(self, held)
        except BaseException:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            async_lock.release()
            raise

    def validate_authority(
        self,
        identity: WorkspaceIdentity,
        owner: WorkspaceOwner,
        fencing_token: str,
    ) -> bool:
        """Runner-side contract: only the currently persisted token authorizes work."""
        try:
            owner.validate()
            _validate_current_identity(identity)
            generation, raw_token = _parse_token(fencing_token)
        except (OSError, ValueError, TypeError):
            return False
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT generation, owner_id, workflow_id, task_id, token_hash, status "
                "FROM workspace_lease_generations WHERE workspace_key = ? "
                "ORDER BY generation DESC LIMIT 1",
                (identity.key,),
            ).fetchone()
        return (
            row is not None
            and row["generation"] == generation
            and row["status"] == "active"
            and row["owner_id"] == owner.owner_id
            and row["workflow_id"] == owner.workflow_id
            and row["task_id"] == owner.task_id
            and secrets.compare_digest(row["token_hash"], token_hash)
        )

    def _latest_active(self, workspace_key: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT generation, owner_id, workflow_id, task_id, label, status "
                "FROM workspace_lease_generations WHERE workspace_key = ? "
                "ORDER BY generation DESC LIMIT 1",
                (workspace_key,),
            ).fetchone()
        return row if row is not None and row["status"] == "active" else None

    async def _release(self, held: _HeldLease) -> None:
        key = held.identity.key
        if self._held.get(key) is not held:
            raise WorkspaceLeaseError("Workspace lease is no longer authoritative")
        held.references -= 1
        if held.references:
            return
        try:
            with self.database.connect() as connection:
                cursor = connection.execute(
                    "UPDATE workspace_lease_generations SET status = 'released', released_at = ? "
                    "WHERE workspace_key = ? AND generation = ? AND owner_id = ? "
                    "AND workflow_id = ? AND task_id = ? AND status = 'active'",
                    (
                        int(time.time()), key, held.generation,
                        held.owner.owner_id, held.owner.workflow_id, held.owner.task_id,
                    ),
                )
            if cursor.rowcount != 1:
                raise WorkspaceLeaseError("Workspace lease was revoked before release")
        finally:
            del self._held[key]
            try:
                fcntl.flock(held.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(held.descriptor)
                held.async_lock.release()

    def _start_generation(
        self,
        identity: WorkspaceIdentity,
        owner: WorkspaceOwner,
        recovery_evidence: RecoveryEvidence | None,
    ) -> tuple[int, str]:
        token_secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token_secret.encode("ascii")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    "SELECT generation, owner_id, workflow_id, task_id, label, status "
                    "FROM workspace_lease_generations "
                    "WHERE workspace_key = ? ORDER BY generation DESC LIMIT 1",
                    (identity.key,),
                ).fetchone()
                generation = previous["generation"] + 1 if previous else 1
                if previous is not None and previous["status"] == "active":
                    recovery_request = InterruptedLease(
                        identity,
                        WorkspaceOwner(
                            previous["owner_id"],
                            previous["workflow_id"],
                            previous["task_id"],
                            previous["label"],
                        ),
                        previous["generation"],
                    )
                    if not _valid_recovery_evidence(recovery_request, recovery_evidence):
                        raise LeaseRecoveryRequired(
                            "The prior workspace owner was interrupted; explicit safe recovery is required",
                        )
                    connection.execute(
                        "UPDATE workspace_lease_generations SET status = 'interrupted', released_at = ? "
                        "WHERE workspace_key = ? AND generation = ? AND status = 'active'",
                        (int(time.time()), identity.key, previous["generation"]),
                    )
                connection.execute(
                    "INSERT INTO workspace_lease_generations(workspace_key, generation, "
                    "owner_id, workflow_id, task_id, label, token_hash, acquired_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                    (
                        identity.key, generation, owner.owner_id, owner.workflow_id,
                        owner.task_id, owner.label, token_hash, int(time.time()),
                    ),
                )
                connection.execute(
                    "DELETE FROM workspace_lease_generations WHERE workspace_key = ? "
                    "AND generation <= ?",
                    (identity.key, generation - 128),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return generation, f"{generation}.{token_secret}"


def capture_preimage(
    identity: WorkspaceIdentity,
    repository: RepositoryIndex,
    relative_path: str,
    *,
    max_bytes: int = 1_048_576,
) -> FilePreimage:
    """Capture bounded no-follow file identity/content evidence for later conflict checks."""
    _validate_current_identity(identity)
    if repository.root != identity.canonical_path:
        raise ValueError("Repository reader is not bound to the registered workspace")
    if (
        not isinstance(relative_path, str) or not relative_path or relative_path.startswith("/")
        or relative_path == "."
        or "\\" in relative_path or "\x00" in relative_path
        or ".." in Path(relative_path).parts
        or type(max_bytes) is not int or not 1 <= max_bytes <= repository.limits.max_file_bytes
    ):
        raise ValueError("Expected-preimage request is invalid")
    before = _safe_file_stat(identity.canonical_path, relative_path)
    if before is None:
        if _safe_file_stat(identity.canonical_path, relative_path) is not None:
            raise WorkspaceLeaseError("Workspace file changed while capturing its preimage")
        return FilePreimage(identity.key, relative_path, False, None, None, None, None)
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise ValueError("Expected-preimage target is not a bounded regular file")
    data = repository.read_file_bytes(relative_path, max_bytes=max_bytes)
    if data is None:
        raise WorkspaceLeaseError("Workspace file disappeared while capturing its preimage")
    after = _safe_file_stat(identity.canonical_path, relative_path)
    if after is None:
        raise WorkspaceLeaseError("Workspace file disappeared while capturing its preimage")
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev, after.st_ino, after.st_size,
    ):
        raise WorkspaceLeaseError("Workspace file changed while capturing its preimage")
    return FilePreimage(
        identity.key,
        relative_path,
        True,
        after.st_dev,
        after.st_ino,
        len(data),
        hashlib.sha256(data).hexdigest(),
    )


def preimage_is_current(
    identity: WorkspaceIdentity,
    repository: RepositoryIndex,
    expected: FilePreimage,
    *,
    max_bytes: int = 1_048_576,
) -> bool:
    if expected.workspace_identity != identity.key or repository.root != identity.canonical_path:
        return False
    try:
        current = capture_preimage(
            identity, repository, expected.path, max_bytes=max_bytes,
        )
    except (OSError, ValueError, WorkspaceLeaseError):
        return False
    return current == expected


def _parse_token(value: str) -> tuple[int, str]:
    if not isinstance(value, str) or len(value) > 128 or "." not in value:
        raise ValueError("Invalid fencing token")
    generation_text, token = value.split(".", 1)
    if not generation_text.isdecimal() or not token or not token.isascii():
        raise ValueError("Invalid fencing token")
    return int(generation_text), token


def _valid_recovery_evidence(
    interrupted: InterruptedLease,
    evidence: RecoveryEvidence | None,
) -> bool:
    return (
        evidence is not None
        and evidence.workspace_identity == interrupted.workspace_identity.key
        and evidence.owner_id == interrupted.owner.owner_id
        and evidence.workflow_id == interrupted.owner.workflow_id
        and evidence.task_id == interrupted.owner.task_id
        and evidence.generation == interrupted.generation
        and evidence.process_tree_reaped is True
        and isinstance(evidence.evidence_id, str)
        and 1 <= len(evidence.evidence_id) <= 128
        and type(evidence.confirmed_at) is int
        and evidence.confirmed_at > 0
    )


def _validate_current_identity(identity: WorkspaceIdentity) -> None:
    path = identity.canonical_path
    expected_key = f"{identity.owner_uid}:{identity.device_id}:{identity.inode}:{path}"
    if identity.key != expected_key:
        raise ValueError("Workspace identity key does not match its canonical filesystem identity")
    if path.resolve(strict=True) != path:
        raise ValueError("Workspace canonical path changed")
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino, info.st_uid)
        != (identity.device_id, identity.inode, identity.owner_uid)
    ):
        raise ValueError("Workspace identity changed")


def _safe_file_stat(root: Path, relative_path: str) -> os.stat_result | None:
    parts = Path(relative_path).parts
    descriptor = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        try:
            return os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            return None
        raise
    finally:
        os.close(descriptor)


def _validate_private_directory(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Workspace lease directory must be private")


def _validate_private_file(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Workspace lease file must be private")


class WorkspaceCoordinatorProtocol(Protocol):
    """Structural contract for future mutation consumers."""

    async def acquire(
        self,
        identity: WorkspaceIdentity,
        owner: WorkspaceOwner,
        *,
        timeout: float = 5,
        recover_interrupted: bool = False,
        recovery_authority: InterruptedOwnerRecovery | None = None,
    ) -> WorkspaceLease: ...

    def validate_authority(
        self,
        identity: WorkspaceIdentity,
        owner: WorkspaceOwner,
        fencing_token: str,
    ) -> bool: ...
