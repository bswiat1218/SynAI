from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from synai.config import Settings
from synai.models import Session
from synai.storage import ConversationStorage


class ExecutionBackend(Protocol):
    settings: Settings
    workspace: Path | None

    def matches(self, session: Session) -> bool: ...

    async def execute(
        self, name: str, arguments: dict[str, Any], expected_sha256: str | None = None,
    ) -> dict[str, Any]: ...


def validate_workspace(workspace: Path, settings: Settings, *, sandbox: bool = False) -> Path:
    if sandbox:
        return ConversationStorage(settings.history_dir).validate_workspace(workspace)
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be an existing directory")
    if workspace in {Path("/"), Path.home().resolve()}:
        raise ValueError("Do not expose root or your entire home directory")
    if settings.history_dir.resolve().is_relative_to(workspace):
        raise ValueError("Workspace cannot expose the history directory")
    if workspace.is_relative_to(settings.history_dir.resolve()):
        raise ValueError("Host workspace must be outside private SynAI storage")
    return workspace
