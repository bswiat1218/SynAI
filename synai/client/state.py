from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any


class ClientStateError(Exception):
    """A local Client Agent state or permission error."""


class ClientState:
    def __init__(self, home: Path | None = None) -> None:
        override = os.environ.get("SYNAI_CLIENT_HOME")
        selected = home or (Path(override) if override else Path.home() / ".config" / "synai-client")
        if not selected.expanduser().is_absolute():
            raise ClientStateError("Client state directory must be an absolute path.")
        self.home = selected.expanduser()
        self.config_path = self.home / "config.json"
        self.identity_path = self.home / "identity.json"
        self.workspaces_path = self.home / "workspaces.json"

    def initialize(self) -> None:
        self._verify_parent_chain(allow_missing=True)
        if self.home.exists() or self.home.is_symlink():
            self._verify_directory(self.home)
        else:
            self.home.mkdir(parents=True, mode=0o700)
            os.chmod(self.home, 0o700)
        self._verify_parent_chain()
        if not self.workspaces_path.exists():
            self._write_json(self.workspaces_path, {"schema_version": 1, "bindings": []})

    def read_config(self) -> dict[str, Any]:
        return self._read_json(self.config_path)

    def write_config(self, value: dict[str, Any]) -> None:
        self._write_json(self.config_path, value)

    def read_identity(self) -> dict[str, Any]:
        return self._read_json(self.identity_path)

    def write_identity(self, value: dict[str, Any]) -> None:
        self._write_json(self.identity_path, value)

    def read_workspaces(self) -> dict[str, Any]:
        return self._read_json(self.workspaces_path)

    def write_workspaces(self, value: dict[str, Any]) -> None:
        self._write_json(self.workspaces_path, value)

    def _read_json(self, path: Path) -> dict[str, Any]:
        self.initialize()
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == 2:
                raise ClientStateError(f"Client state is missing: {path.name}") from exc
            if exc.errno in {20, 40}:
                raise ClientStateError(f"Client state permissions or file type are unsafe: {path.name}") from exc
            raise ClientStateError(f"Client state could not be opened: {path.name}") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 1024 * 1024
            ):
                raise ClientStateError(f"Client state permissions or file type are unsafe: {path.name}")
            pieces: list[bytes] = []
            remaining = 1024 * 1024 + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                pieces.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(pieces)
            if len(raw) > 1024 * 1024:
                raise ClientStateError(f"Client state exceeds its supported size: {path.name}")
        except OSError as exc:
            raise ClientStateError(f"Client state could not be read: {path.name}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClientStateError(f"Client state could not be read: {path.name}") from exc
        if not isinstance(value, dict):
            raise ClientStateError(f"Client state has an invalid format: {path.name}")
        return value

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self.initialize_directory()
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ClientStateError("Client state exceeds its supported size.")
        if path.exists() or path.is_symlink():
            try:
                info = path.lstat()
            except OSError as exc:
                raise ClientStateError(f"Client state could not be inspected: {path.name}") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ClientStateError(f"Client state permissions or file type are unsafe: {path.name}")
        temporary = self.home / f".{path.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory()
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def initialize_directory(self) -> None:
        self._verify_parent_chain(allow_missing=True)
        if not self.home.exists():
            self.home.mkdir(parents=True, mode=0o700)
            os.chmod(self.home, 0o700)
        self._verify_parent_chain()
        self._verify_directory(self.home)

    def _verify_parent_chain(self, allow_missing: bool = False) -> None:
        current = Path(self.home.anchor)
        for part in self.home.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return
                raise ClientStateError("Client state directory path is unavailable.")
            except OSError as exc:
                raise ClientStateError("Client state directory path is unsafe.") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ClientStateError("Client state directory path cannot contain symbolic links.")

    @staticmethod
    def _verify_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ClientStateError(f"Client state is missing: {path.name}") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ClientStateError("Client state directory must be user-owned and mode 0700.")

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
