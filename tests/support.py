from __future__ import annotations

from synai.config import ConversationEnvironment
from synai.models import Session
from synai.storage import ConversationStorage
from synai.sandbox import Sandbox


def bind_sandbox(sandbox: Sandbox, session: Session) -> None:
    storage = ConversationStorage(sandbox.settings.history_dir)
    storage.create(session.session_id, workspace=True)
    session.schema_version = 4
    workspace = storage.workspace(session.session_id)
    session.set_environment(ConversationEnvironment.from_settings(sandbox.settings, workspace))
    session.managed_workspace_created = True
    sandbox.workspace = workspace
