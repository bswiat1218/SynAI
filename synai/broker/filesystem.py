from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
import weakref
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from synai.broker.protocol import BrokerError


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


def open_beneath(directory: int, name: str, flags: int, mode: int = 0,
                 *, no_xdev: bool = True) -> int:
    if not name or name.startswith("/") or any(part in {"", ".."} for part in name.split("/")):
        raise BrokerError("filesystem_path_invalid", "Descriptor-relative path is invalid.", 409)
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise BrokerError("openat2_unavailable", "Required Linux path resolution is unsupported.", 503)
    # BENEATH | NO_SYMLINKS | NO_MAGICLINKS, plus NO_XDEV inside an anchored mount.
    how = _OpenHow(flags | os.O_CLOEXEC | os.O_NOFOLLOW, mode, 0x08 | 0x04 | 0x02 | (0x01 if no_xdev else 0))
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(437), ctypes.c_int(directory),
                          ctypes.c_char_p(os.fsencode(name)), ctypes.byref(how), ctypes.sizeof(how))
    if result < 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.EINVAL, errno.EPERM}:
            raise BrokerError("openat2_unavailable", "Required Linux path resolution is unavailable.", 503)
        raise OSError(code, os.strerror(code), name)
    return int(result)


def mount_identity(descriptor: int) -> tuple[int, int, int]:
    try:
        text = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
        mount = int(next(line.split(":", 1)[1] for line in text.splitlines() if line.startswith("mnt_id:")))
    except (OSError, ValueError, StopIteration) as exc:
        raise BrokerError("filesystem_identity_unavailable", "Kernel mount identity is unavailable.", 503) from exc
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino, mount


def filesystem_magic(descriptor: int) -> int:
    data = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(descriptor, data) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return ctypes.c_long.from_buffer(data).value


class Directory:
    """Owned dirfd with pinned ancestry; path is diagnostic, never child authority."""

    def __init__(self, path: Path, *, owner_uid: int | None = None,
                 owner_gid: int | None = None, private: bool = False) -> None:
        if not path.is_absolute() or path != Path(os.path.normpath(path)):
            raise BrokerError("filesystem_path_invalid", "Authority path must be canonical and absolute.", 409)
        self.path = path
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.owner_gid = owner_gid
        self.private = private
        self._chain: list[int] = []
        self._identities: list[tuple[int, int, int]] = []
        try:
            current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self._chain.append(current)
            for part in path.parts[1:]:
                current = open_beneath(current, part, os.O_RDONLY | os.O_DIRECTORY, no_xdev=False)
                self._chain.append(current)
            self._identities = [mount_identity(fd) for fd in self._chain]
            self._check_permissions()
            self._finalizer = weakref.finalize(self, _close_descriptors, self._chain)
        except BaseException:
            self.close()
            raise

    @classmethod
    def child(cls, parent: Directory, name: str) -> Directory:
        parent.revalidate()
        descriptor = open_beneath(parent.fd, name, os.O_RDONLY | os.O_DIRECTORY)
        instance = cls.__new__(cls)
        instance.path = parent.path / name
        instance.owner_uid = parent.owner_uid
        instance.owner_gid = parent.owner_gid
        instance.private = False
        instance._chain = [os.dup(fd) for fd in parent._chain]
        instance._chain.append(descriptor)
        instance._identities = [mount_identity(fd) for fd in instance._chain]
        instance._finalizer = weakref.finalize(instance, _close_descriptors, instance._chain)
        try:
            instance._check_permissions()
        except BaseException:
            instance.close()
            raise
        return instance

    @property
    def fd(self) -> int:
        if not self._chain:
            raise BrokerError("filesystem_authority_closed", "Filesystem authority is closed.", 503)
        return self._chain[-1]

    def _check_permissions(self) -> None:
        for index, fd in enumerate(self._chain):
            info = os.fstat(fd)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, self.owner_uid} or (
                info.st_mode & 0o022 and not sticky_root
            ):
                raise BrokerError("filesystem_ownership_invalid", "Filesystem ancestor is untrusted.", 409)
            if index == len(self._chain) - 1 and (
                info.st_uid != self.owner_uid
                or self.owner_gid is not None and info.st_gid != self.owner_gid
                or self.private and stat.S_IMODE(info.st_mode) != 0o700
                or info.st_mode & 0o022
            ):
                raise BrokerError("filesystem_ownership_invalid", "Filesystem directory ownership is unsafe.", 409)

    def revalidate(self) -> None:
        self._check_permissions()
        if [mount_identity(fd) for fd in self._chain] != self._identities:
            raise BrokerError("filesystem_identity_changed", "Pinned filesystem identity changed.", 409)
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            if mount_identity(current) != self._identities[0]:
                raise BrokerError("filesystem_identity_changed", "Filesystem ancestry changed.", 409)
            for index, part in enumerate(self.path.parts[1:], 1):
                following = open_beneath(current, part, os.O_RDONLY | os.O_DIRECTORY, no_xdev=False)
                os.close(current)
                current = following
                if mount_identity(current) != self._identities[index]:
                    raise BrokerError("filesystem_identity_changed", "Filesystem pathname was substituted.", 409)
        finally:
            os.close(current)

    def open(self, name: str, flags: int, mode: int = 0) -> int:
        self.revalidate()
        return open_beneath(self.fd, name, flags, mode)

    def child_directory(self, name: str) -> Directory:
        return Directory.child(self, name)

    def mkdir(self, name: str) -> Directory:
        if "/" in name or name in {"", ".", ".."}:
            raise BrokerError("filesystem_path_invalid", "Directory creation requires one component.", 409)
        self.revalidate()
        os.mkdir(name, mode=0o700, dir_fd=self.fd)
        os.fsync(self.fd)
        return self.child_directory(name)

    def close(self) -> None:
        _close_descriptors(self._chain)

    def __enter__(self) -> Directory:
        self.revalidate()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@contextmanager
def directory_access(value: Path | Directory) -> Iterator[Directory]:
    if isinstance(value, Directory):
        value.revalidate()
        yield value
        value.revalidate()
    else:
        with Directory(value) as directory:
            yield directory
            directory.revalidate()


def read_file(directory: Directory, name: str, maximum: int) -> bytes:
    descriptor = directory.open(name, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_uid != directory.owner_uid:
            raise BrokerError("filesystem_file_invalid", "Only bounded owned regular files are supported.", 409)
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise BrokerError("filesystem_file_limit", "File exceeds its bound.", 413)
        return bytes(data)
    finally:
        os.close(descriptor)


def write_new(directory: Directory, name: str, data: bytes, mode: int = 0o600) -> None:
    descriptor = directory.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Filesystem write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory.revalidate()
    os.fsync(directory.fd)


def walk_files(directory: Directory, prefix: str = "") -> Iterator[tuple[str, bytes]]:
    if prefix.count("/") > 16:
        raise BrokerError("artifact_limit", "Filesystem traversal exceeds its depth bound.", 413)
    directory.revalidate()
    names = sorted(os.listdir(directory.fd))
    if len(names) > 500:
        raise BrokerError("artifact_limit", "Filesystem traversal exceeds its entry bound.", 413)
    for name in names:
        info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with directory.child_directory(name) as child:
                yield from walk_files(child, prefix + name + "/")
        elif stat.S_ISREG(info.st_mode):
            yield prefix + name, read_file(directory, name, 1024 * 1024)
        else:
            raise BrokerError("artifact_unsupported_file", "Workspace contains unsupported filesystem entries.", 422)


def remove_tree(directory: Directory) -> None:
    # Refuse the complete tree before deleting anything when an entry is unsafe.
    for _ in walk_files(directory):
        pass
    directory.revalidate()
    for name in os.listdir(directory.fd):
        info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with directory.child_directory(name) as child:
                os.fchmod(child.fd, 0o700)
                remove_tree(child)
                child.revalidate()
                os.rmdir(name, dir_fd=directory.fd)
        elif stat.S_ISREG(info.st_mode) and info.st_uid == directory.owner_uid:
            descriptor = directory.open(name, os.O_RDONLY | os.O_NONBLOCK)
            try:
                if mount_identity(descriptor)[:2] != (info.st_dev, info.st_ino):
                    raise BrokerError("filesystem_identity_changed", "Cleanup entry was substituted.", 409)
                os.unlink(name, dir_fd=directory.fd)
            finally:
                os.close(descriptor)
        else:
            raise BrokerError("workspace_cleanup_refused", "Cleanup encountered untrusted content.", 409)
    os.fsync(directory.fd)


def _close_descriptors(descriptors: list[int]) -> None:
    while descriptors:
        os.close(descriptors.pop())
