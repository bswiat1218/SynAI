from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping

from synai.config import Settings
from synai.storage import ConversationStorage, checked_path, write_private_json


DEFAULT_THEME = "synai-cyberpunk"


@dataclass(frozen=True)
class Preferences:
    ollama_url: str = "http://localhost:11434"
    request_timeout: float = 1200
    theme: str = DEFAULT_THEME
    schema_version: int = 1

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported application settings version")
        if not isinstance(self.theme, str) or not self.theme or not self.theme.strip() == self.theme:
            raise ValueError("Theme must be a nonempty name")
        Settings(ollama_url=self.ollama_url, request_timeout=self.request_timeout).validate()


class PreferencesStore:
    def __init__(self, root: Path) -> None:
        self.storage = ConversationStorage(root)
        self.path = checked_path(self.storage.root / "settings.json")

    def load(self) -> Preferences:
        self.storage.initialize()
        checked_path(self.path)
        if not self.path.exists():
            return Preferences()
        try:
            info = self.path.stat()
            if not self.path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Application settings must be a private file owned by your user")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Application settings must be an object")
            if set(value) != {"schema_version", "ollama_url", "request_timeout", "theme"}:
                raise ValueError("Invalid application settings fields")
            result = Preferences(**value)
            result.validate()
            return result
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"Cannot load {self.path}: {exc}") from exc

    def save(self, preferences: Preferences) -> None:
        preferences.validate()
        self.storage.initialize()
        write_private_json(self.path, asdict(preferences))


def resolve_connection(
    settings: Settings, saved: Preferences, *, cli_url: str | None = None,
    cli_timeout: float | None = None, environ: Mapping[str, str] | None = None,
) -> tuple[Settings, dict[str, str]]:
    environment = os.environ if environ is None else environ
    env_url = environment.get("OLLAMA_URL") or environment.get("OLLAMA_HOST")
    env_timeout = environment.get("AGENT_REQUEST_TIMEOUT", environment.get("BENCHMARK_REQUEST_TIMEOUT"))
    url = cli_url if cli_url is not None else env_url if env_url else saved.ollama_url
    timeout = cli_timeout if cli_timeout is not None else float(env_timeout) if env_timeout is not None else saved.request_timeout
    result = replace(settings, ollama_url=url.rstrip("/"), request_timeout=timeout)
    result.validate()
    sources = {
        "ollama_url": "CLI" if cli_url is not None else "environment" if env_url else "saved/default",
        "request_timeout": "CLI" if cli_timeout is not None else "environment" if env_timeout is not None else "saved/default",
    }
    return result, sources
