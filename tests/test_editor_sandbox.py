from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from synai.config import Settings
from synai.sandbox import Sandbox
from synai.storage import ConversationStorage
from synai.editor.environment import EditorContext, cleanup, prepare, run
from synai.editor.neovim import Neovim
from synai.editor.protocol import EditorError


@unittest.skipUnless(os.environ.get("SYNAI_TEST_EDITOR_IMAGE"),
                     "Set SYNAI_TEST_EDITOR_IMAGE only after approving temporary test container creation")
class SandboxEditorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restricted_sandbox_neovim_file_mapping_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings(), history_dir=Path(directory) / "history",
                               image=os.environ["SYNAI_TEST_EDITOR_IMAGE"])
            storage = ConversationStorage(settings.history_dir)
            identifier = uuid4().hex
            storage.create(identifier, workspace=True)
            workspace = storage.workspace(identifier)
            file = workspace / "sandbox file.py"
            file.write_text("sandbox original\n")
            sandbox = Sandbox(settings)
            await sandbox.create(workspace, settings.image)
            try:
                self.assertTrue(sandbox.healthy)
                context = EditorContext(
                    identifier, str(workspace), "sandbox", settings.runtime,
                    sandbox.container_id, sandbox.uid, "/opt/synai/mini.nvim")
                staging = await asyncio.to_thread(prepare, context)
                nvim = Neovim(context, staging)
                command = nvim.spawn("nvim")
                command[command.index("-it")] = "-i"
                command.insert(command.index("nvim", command.index("env") + 1) + 1, "--headless")
                process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            await asyncio.to_thread(nvim.call, "state")
                            break
                        except EditorError:
                            if time.monotonic() >= deadline:
                                raise
                            await asyncio.sleep(0.1)
                    await asyncio.to_thread(nvim.call, "open", {"path": context.file_path(str(file))})
                    opened = await asyncio.to_thread(
                        run, context, "nvim", "--server", nvim.socket, "--remote-expr", "expand('%:p')")
                    self.assertEqual(opened.strip(), "/workspace/sandbox file.py")
                    identity = json.loads(await asyncio.to_thread(
                        run, context, "python3", "-c",
                        "import os,json;print(json.dumps([os.geteuid(),os.getcwd()]))"))
                    self.assertEqual(identity, [sandbox.uid, "/workspace"])
                    await asyncio.to_thread(
                        run, context, "nvim", "--server", nvim.socket, "--remote-expr",
                        "luaeval('vim.api.nvim_buf_set_lines(0,0,-1,false,{\"sandbox edited\"})')")
                    await asyncio.to_thread(nvim.call, "save")
                    self.assertEqual(file.read_text(), "sandbox edited\n")
                    await asyncio.to_thread(cleanup, context, staging)
                    await asyncio.to_thread(process.communicate, timeout=10)
                    await sandbox.validate()
                    self.assertTrue(sandbox.healthy, "Editor cleanup must not terminate container keeper")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        await asyncio.to_thread(process.communicate, timeout=10)
            finally:
                await sandbox.remove_owned()


if __name__ == "__main__":
    unittest.main()
