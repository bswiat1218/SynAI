"""Exercise installed runtime behavior without a source checkout on sys.path."""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from unittest.mock import AsyncMock, patch


async def main() -> None:
    import synai
    from synai.config import ConversationEnvironment, Settings
    from synai.editor.environment import ASSETS
    from synai.host import HostExecution
    from synai.models import ModelInfo, Session
    from synai.preferences import Preferences
    from synai.storage import ConversationStorage
    from synai.tui.application import CodingApp

    assert metadata.version("synai") == synai.__version__
    installed = Path(synai.__file__).resolve().parent
    assert "site-packages" in installed.parts, f"Not an installed package: {installed}"
    assert files("synai.tui").joinpath("theme.tcss").read_text()
    for asset in (*ASSETS, "sandbox-editor.Dockerfile"):
        assert files("synai.editor").joinpath(asset).read_text()

    class Provider:
        def __init__(self, *_args) -> None:
            self.close = AsyncMock()

        async def list_models(self):
            return [ModelInfo("smoke-model")]

    with tempfile.TemporaryDirectory(prefix="synai-smoke-") as directory:
        root = Path(directory)
        settings = replace(Settings(), history_dir=root / "history", execution_mode="host")
        with patch("synai.tui.application.OllamaProvider", Provider):
            app = CodingApp(settings, preferences=Preferences())
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                assert app.screen.is_mounted
                assert app.models["smoke-model"].name == "smoke-model"
        workspace = root / "workspace"
        workspace.mkdir()
        session = Session("smoke-model", settings.ollama_url, str(workspace))
        session.set_environment(ConversationEnvironment.from_settings(settings, workspace))
        host = HostExecution(settings)
        host.activate(session)
        result = await host.execute("write_file", {"path": "installed.txt", "content": "installed helper"})
        assert result["ok"], result
        assert (workspace / "installed.txt").read_text() == "installed helper"
        host.revoke()
        storage = ConversationStorage(settings.history_dir)
        storage.initialize()
        assert storage.root.is_dir()
    print(f"Installed runtime smoke passed: SynAI {synai.__version__}")


if __name__ == "__main__":
    if os.geteuid() == 0:
        raise SystemExit("Release smoke must run as a non-root Linux user")
    asyncio.run(main())
