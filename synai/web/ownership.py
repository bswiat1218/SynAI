from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from synai.storage import ConversationStorage, checked_path


class OwnershipConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class LockOwner:
    pid: int
    mode: str
    started_at: int


class DataRootOwnership:
    """OS-backed single-process ownership for the shared ~/.synai data root."""

    def __init__(self, data_root: Path, mode: str) -> None:
        if mode not in {"tui", "web"}:
            raise ValueError("Data-root owner mode must be tui or web")
        self.storage = ConversationStorage(data_root)
        self.path = checked_path(self.storage.root / "application.lock")
        self.mode = mode
        self._descriptor: int | None = None
        self._owner: LockOwner | None = None

    def acquire(self) -> LockOwner:
        if self._descriptor is not None:
            raise RuntimeError("Data-root ownership is already held")
        self.storage.initialize()
        checked_path(self.path)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Data-root lock must be a private regular file owned by this user")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                current = self._read_owner(descriptor)
                description = (
                    f"PID {current.pid} ({current.mode})"
                    if current is not None else "another SynAI process"
                )
                raise OwnershipConflict(
                    f"SynAI data root is already owned by {description}; stop that process first.",
                ) from exc
            owner = LockOwner(os.getpid(), self.mode, int(time.time()))
            metadata = json.dumps({
                "pid": owner.pid,
                "mode": owner.mode,
                "started_at": owner.started_at,
                "instance": secrets.token_hex(16),
            }, separators=(",", ":")).encode("ascii")
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, metadata, 0)
            os.fsync(descriptor)
            self._descriptor, self._owner = descriptor, owner
            return owner
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        self._owner = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> DataRootOwnership:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    @staticmethod
    def _read_owner(descriptor: int) -> LockOwner | None:
        try:
            value = json.loads(os.pread(descriptor, 4096, 0))
            pid, mode, started_at = value["pid"], value["mode"], value["started_at"]
            if (
                type(pid) is not int or pid < 1 or mode not in {"tui", "web"}
                or type(started_at) is not int or started_at < 0
            ):
                return None
            return LockOwner(pid, mode, started_at)
        except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None
