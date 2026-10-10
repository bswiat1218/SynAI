from __future__ import annotations

import hashlib
import os
import stat
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from synai.client.state import ClientState, ClientStateError


MAX_FILES = 500
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PATH_DEPTH = 16
MAX_PATH_BYTES = 512
SUPPORTED_EXTENSIONS = frozenset({
    ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".ini", ".cfg",
    ".yaml", ".yml", ".json", ".xml", ".html", ".css", ".js", ".jsx",
    ".ts", ".tsx", ".sh", ".sql", ".go", ".rs", ".java", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua",
})
SUPPORTED_NAMES = frozenset({
    "Dockerfile", "Makefile", "GNUmakefile", "Justfile", "Procfile",
    ".gitignore", ".dockerignore", ".editorconfig", ".coveragerc",
    ".env.example", ".env.sample", ".env.template",
})
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", ".cache", "dist", "build", "htmlcov", "coverage",
    "site-packages", ".ssh", ".aws",
})
EXCLUDED_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_ed25519", "credentials", "secrets",
})
EXCLUDED_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der"})


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class CapturedFile:
    path: str
    data: bytes
    sha256: str
    device: int
    inode: int
    mtime_ns: int

    def manifest(self) -> dict[str, object]:
        return {
            "path": self.path,
            "file_type": "regular",
            "size_bytes": len(self.data),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Preview:
    files: tuple[CapturedFile, ...]
    excluded: tuple[tuple[str, str], ...]
    rejected: tuple[tuple[str, str], ...]
    total_bytes: int


class WorkspaceRegistry:
    def __init__(self, state: ClientState) -> None:
        self.state = state

    def add(self, project_id: str, binding_id: str, alias: str, source: Path) -> dict[str, Any]:
        if not _opaque_id(project_id) or not _opaque_id(binding_id):
            raise WorkspaceError("Project and binding IDs must be opaque 32-character IDs.")
        if not alias.strip() or len(alias) > 128 or any(ord(char) < 32 for char in alias):
            raise WorkspaceError("Workspace alias must contain 1 to 128 printable characters.")
        absolute = Path(os.path.abspath(source))
        _reject_symlink_components(absolute)
        canonical = absolute
        descriptor = _open_directory(canonical)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                raise WorkspaceError("Workspace must be a directory.")
            record = {
                "binding_id": binding_id,
                "project_id": project_id,
                "alias": alias.strip(),
                "path": str(canonical),
                "device": info.st_dev,
                "inode": info.st_ino,
                "capabilities": ["snapshot-v1"],
                "registered_at": int(time.time()),
                "revoked_at": None,
                "status": "active",
            }
        finally:
            os.close(descriptor)
        registry = self.state.read_workspaces()
        bindings = registry.get("bindings")
        if not isinstance(bindings, list):
            raise ClientStateError("Local workspace registry is malformed.")
        if any(item.get("binding_id") == binding_id for item in bindings if isinstance(item, dict)):
            raise WorkspaceError("Workspace binding is already registered locally.")
        bindings.append(record)
        self.state.write_workspaces({"schema_version": 1, "bindings": bindings})
        return record

    def list(self) -> list[dict[str, Any]]:
        records = self.state.read_workspaces().get("bindings")
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ClientStateError("Local workspace registry is malformed.")
        return records

    def remove(self, binding_id: str) -> None:
        records = self.list()
        found = False
        for item in records:
            if item.get("binding_id") == binding_id and item.get("status") == "active":
                item["status"] = "revoked"
                item["revoked_at"] = int(time.time())
                item.pop("path", None)
                found = True
        if not found:
            raise WorkspaceError("Local workspace binding was not found.")
        self.state.write_workspaces({"schema_version": 1, "bindings": records})

    def find(self, binding_id: str) -> dict[str, Any]:
        for record in self.list():
            if (
                record.get("binding_id") == binding_id
                and record.get("status") == "active"
                and record.get("capabilities") == ["snapshot-v1"]
            ):
                return record
        raise WorkspaceError("Workspace binding is not locally authorized.")


def capture_workspace(binding: dict[str, Any]) -> Preview:
    root = Path(str(binding.get("path", "")))
    expected_device = binding.get("device")
    expected_inode = binding.get("inode")
    root_fd = _open_directory(root)
    try:
        root_before = os.fstat(root_fd)
        _check_identity(root_before, expected_device, expected_inode, "Workspace root changed since registration.")
        files: list[CapturedFile] = []
        excluded: list[tuple[str, str]] = []
        rejected: list[tuple[str, str]] = []
        seen: set[str] = set()
        _scan_directory(root_fd, "", files, excluded, rejected, seen)
        _check_directory_unchanged(root_fd, root_before, "Workspace root changed during preview.")
    finally:
        os.close(root_fd)
    total = sum(len(item.data) for item in files)
    if len(files) > MAX_FILES:
        rejected.append((".", f"Snapshot exceeds {MAX_FILES} files."))
    if total > MAX_TOTAL_BYTES:
        rejected.append((".", f"Snapshot exceeds {MAX_TOTAL_BYTES} bytes."))
    return Preview(tuple(files), tuple(excluded), tuple(rejected), total)


def _scan_directory(
    directory_fd: int,
    prefix: str,
    files: list[CapturedFile],
    excluded: list[tuple[str, str]],
    rejected: list[tuple[str, str]],
    seen: set[str],
) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise WorkspaceError("Workspace changed or became unreadable during preview.") from exc
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        if name in {"", ".", ".."} or "/" in name or "\\" in name:
            rejected.append((relative, "Unsafe path component."))
            continue
        if not _safe_relative_path(relative):
            rejected.append((relative, "Path is not a safe portable workspace-relative path."))
            continue
        if prefix and any(part in EXCLUDED_DIRECTORIES for part in prefix.split("/")):
            excluded.append((relative, "Excluded development or credential directory."))
            continue
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            rejected.append((relative, "Entry changed during preview."))
            continue
        if stat.S_ISLNK(info.st_mode):
            excluded.append((relative, "Symbolic links are never followed."))
            continue
        if stat.S_ISDIR(info.st_mode):
            if name in EXCLUDED_DIRECTORIES:
                excluded.append((relative, "Excluded development or credential directory."))
                continue
            try:
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError:
                rejected.append((relative, "Directory changed or cannot be opened without following links."))
                continue
            try:
                child_before = os.fstat(child_fd)
                _check_identity(child_before, info.st_dev, info.st_ino, "Directory changed during preview.")
                if child_before.st_mtime_ns != info.st_mtime_ns or child_before.st_ctime_ns != info.st_ctime_ns:
                    raise WorkspaceError("Directory entries changed during preview.")
                _scan_directory(child_fd, relative, files, excluded, rejected, seen)
                _check_directory_unchanged(child_fd, child_before, "Directory changed during preview.")
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                _check_identity(current, info.st_dev, info.st_ino, "Directory path was replaced during preview.")
                if current.st_mtime_ns != info.st_mtime_ns or current.st_ctime_ns != info.st_ctime_ns:
                    raise WorkspaceError("Directory entries changed during preview.")
            finally:
                os.close(child_fd)
            continue
        reason = _file_exclusion(relative, info)
        if reason:
            (excluded if "Excluded" in reason else rejected).append((relative, reason))
            continue
        canonical = unicodedata.normalize("NFC", relative).casefold()
        if canonical in seen:
            rejected.append((relative, "Path collides with another path after normalization."))
            continue
        seen.add(canonical)
        if len(files) >= MAX_FILES:
            rejected.append((relative, f"Snapshot exceeds {MAX_FILES} files."))
            continue
        if sum(len(item.data) for item in files) + info.st_size > MAX_TOTAL_BYTES:
            rejected.append((relative, f"Snapshot exceeds {MAX_TOTAL_BYTES} bytes."))
            continue
        try:
            data, after = _read_file_at(directory_fd, name, info)
        except (OSError, WorkspaceError) as exc:
            rejected.append((relative, str(exc) or "File changed during preview."))
            continue
        if len(data) > MAX_FILE_BYTES:
            rejected.append((relative, f"File exceeds {MAX_FILE_BYTES} bytes."))
            continue
        if b"\0" in data:
            rejected.append((relative, "Binary content is not supported."))
            continue
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            rejected.append((relative, "File is not valid UTF-8 text."))
            continue
        if sum(len(item.data) for item in files) + len(data) > MAX_TOTAL_BYTES:
            rejected.append((relative, f"Snapshot exceeds {MAX_TOTAL_BYTES} bytes."))
            continue
        files.append(CapturedFile(
            relative, data, hashlib.sha256(data).hexdigest(),
            after.st_dev, after.st_ino, after.st_mtime_ns,
        ))


def _file_exclusion(relative: str, info: os.stat_result) -> str | None:
    name = PurePosixPath(relative).name
    lower = name.lower()
    suffix = PurePosixPath(relative).suffix.lower()
    if name in EXCLUDED_NAMES or (lower.startswith(".env.") and lower not in {
        ".env.example", ".env.sample", ".env.template",
    }) or suffix in EXCLUDED_EXTENSIONS:
        return "Excluded secret-bearing or credential file."
    if not stat.S_ISREG(info.st_mode):
        return "Unsupported special file; only regular text files are allowed."
    if suffix not in SUPPORTED_EXTENSIONS and name not in SUPPORTED_NAMES:
        return "Unsupported file type for the snapshot protocol."
    return None


def _read_file_at(parent_fd: int, name: str, expected: os.stat_result) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        _check_identity(before, expected.st_dev, expected.st_ino, "File identity changed during preview.")
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceError("File is not a regular file.")
        if before.st_size > MAX_FILE_BYTES:
            return b"\0" * (MAX_FILE_BYTES + 1), before
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        _check_identity(after, before.st_dev, before.st_ino, "File identity changed during preview.")
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or len(data) != after.st_size
        ):
            raise WorkspaceError("File was modified during preview; create a new preview.")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_identity(current, before.st_dev, before.st_ino, "File path was replaced during preview.")
        return data, after
    finally:
        os.close(descriptor)


def _open_directory(path: Path) -> int:
    if not path.is_absolute():
        raise WorkspaceError("Authorized workspace path must be absolute.")
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        for component in path.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            previous = descriptor
            descriptor = child
            os.close(previous)
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise WorkspaceError("Authorized workspace directory is unavailable.") from exc


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise WorkspaceError("Workspace path is unavailable.") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkspaceError("Workspace path contains a symbolic-link component.")


def _check_identity(info: os.stat_result, device: object, inode: object, message: str) -> None:
    if info.st_dev != device or info.st_ino != inode:
        raise WorkspaceError(message)


def _check_directory_unchanged(descriptor: int, before: os.stat_result, message: str) -> None:
    after = os.fstat(descriptor)
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
    ):
        raise WorkspaceError(message)


def _safe_relative_path(path: str) -> bool:
    try:
        byte_length = len(path.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    parts = path.split("/")
    return (
        bool(path)
        and byte_length <= MAX_PATH_BYTES
        and len(path) <= MAX_PATH_BYTES
        and len(parts) <= MAX_PATH_DEPTH
        and not path.startswith("/")
        and "\\" not in path
        and all(ord(character) >= 32 and ord(character) != 127 for character in path)
        and all(
            part not in {"", ".", ".."}
            and ":" not in part
            and not part.endswith((".", " "))
            and part.split(".", 1)[0].upper() not in reserved
            for part in parts
        )
    )


def _opaque_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)
