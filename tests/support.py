from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from synai.config import ConversationEnvironment
from synai.models import Session
from synai.storage import ConversationStorage
if TYPE_CHECKING:
    from synai.tui.application import CodingApp
    from synai.sandbox import Sandbox


def save_managed(app: CodingApp, session: Session) -> None:
    environment = session.environment or app.history.environment_for(session)
    workspace = (
        app.storage.workspace(session.session_id) if environment.execution_mode == "sandbox"
        else Path(session.workspace)
    )
    session.schema_version = 4
    session.set_environment(replace(environment, workspace=str(workspace)))
    session.managed_workspace_created = environment.execution_mode == "sandbox"
    app.storage.create(session.session_id, workspace=environment.execution_mode == "sandbox")
    app.history.save(session)


def bind_sandbox(sandbox: Sandbox, session: Session) -> None:
    storage = ConversationStorage(sandbox.settings.history_dir)
    storage.create(session.session_id, workspace=True)
    session.schema_version = 4
    workspace = storage.workspace(session.session_id)
    session.set_environment(ConversationEnvironment.from_settings(sandbox.settings, workspace))
    session.managed_workspace_created = True
    sandbox.workspace = workspace
