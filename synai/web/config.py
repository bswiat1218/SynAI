from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from synai.config import Settings


_WORKSPACE_KEY = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_MAX_PASSWORD_BYTES = 1024


@dataclass(frozen=True)
class WorkspaceMountConfig:
    key: str
    host_path: Path


@dataclass(frozen=True)
class WebConfig:
    data_root: Path
    workspace_mounts: tuple[WorkspaceMountConfig, ...]
    initial_password: str | None = field(default=None, repr=False)
    ollama_url: str = "http://127.0.0.1:11434"
    public_origin: str = "http://127.0.0.1:8765"
    bind_host: str = "127.0.0.1"
    port: int = 8765
    session_lifetime_seconds: int = 43_200
    request_limit_bytes: int = 1_048_576
    concurrent_request_limit: int = 64

    @classmethod
    def from_env(cls) -> WebConfig:
        workspace_value = os.environ.get("SYNAI_WORKSPACES", "")
        mounts: list[WorkspaceMountConfig] = []
        if workspace_value:
            for entry in workspace_value.split(os.pathsep):
                key, separator, raw_path = entry.partition("=")
                if not separator or not raw_path:
                    raise ValueError("SYNAI_WORKSPACES must contain key=/absolute/path entries")
                mounts.append(WorkspaceMountConfig(key, Path(raw_path).expanduser()))
        config = cls(
            data_root=Path.home() / ".synai",
            workspace_mounts=tuple(mounts),
            initial_password=os.environ.get("SYNAI_INITIAL_PASSWORD"),
            ollama_url=os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST")
            or "http://127.0.0.1:11434",
            public_origin=os.environ.get("SYNAI_PUBLIC_ORIGIN", "http://127.0.0.1:8765"),
            bind_host=os.environ.get("SYNAI_BIND_HOST", "127.0.0.1"),
            port=_integer_environment("SYNAI_PORT", 8765),
        )
        config.validate()
        return config

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_origin).scheme == "https"

    def validate(self) -> None:
        if not isinstance(self.data_root, Path) or not self.data_root.is_absolute():
            raise ValueError("Web data root must be an absolute path")
        if self.initial_password is not None:
            if not isinstance(self.initial_password, str):
                raise ValueError("Initial credential must be a string")
            encoded = self.initial_password.encode("utf-8")
            if not 12 <= len(encoded) <= _MAX_PASSWORD_BYTES:
                raise ValueError("Initial credential must be 12 to 1024 UTF-8 bytes")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"} or not origin.hostname
            or origin.username or origin.password or origin.path not in {"", "/"}
            or origin.query or origin.fragment
        ):
            raise ValueError("Public origin must be an exact HTTP(S) origin without credentials or path")
        if origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Non-local deployments must use an HTTPS public origin")
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError:
            if self.bind_host != "localhost":
                raise ValueError("Bind host must be localhost or an IP address")
        else:
            if not address.is_loopback and origin.scheme != "https":
                raise ValueError("Non-loopback binds require an HTTPS public origin")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Web port must be between 1 and 65535")
        if (
            type(self.session_lifetime_seconds) is not int
            or not 300 <= self.session_lifetime_seconds <= 2_592_000
        ):
            raise ValueError("Session lifetime must be between five minutes and thirty days")
        if (
            type(self.request_limit_bytes) is not int
            or not 1024 <= self.request_limit_bytes <= 8_388_608
            or type(self.concurrent_request_limit) is not int
            or not 1 <= self.concurrent_request_limit <= 1024
        ):
            raise ValueError("Web request limits are outside their supported bounds")
        settings = Settings(ollama_url=self.ollama_url, history_dir=self.data_root)
        settings.validate()
        seen: set[str] = set()
        if not isinstance(self.workspace_mounts, tuple):
            raise ValueError("Workspace mounts must be an immutable tuple")
        for mount in self.workspace_mounts:
            if (
                not isinstance(mount, WorkspaceMountConfig)
                or not _WORKSPACE_KEY.fullmatch(mount.key)
                or mount.key in seen
                or not isinstance(mount.host_path, Path)
                or not mount.host_path.is_absolute()
            ):
                raise ValueError("Invalid or duplicate configured workspace mount")
            seen.add(mount.key)
        if len(self.workspace_mounts) > 256:
            raise ValueError("At most 256 workspace roots may be configured")


def _integer_environment(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
