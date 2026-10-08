from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from synai.editor.environment import EditorContext, cleanup, prepare, run
from synai.editor.neovim import Neovim
from synai.editor.protocol import EditorError


class NeovimIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.context = EditorContext("integration", self.workspace.name, "host",
                                     uid=os.getuid())
        self.directory = prepare(self.context)
        self.nvim = Neovim(self.context, self.directory)
        command = self.nvim.spawn("nvim")
        command.insert(command.index(self.nvim.binary) + 1, "--headless")
        self.process = subprocess.Popen(
            command, start_new_session=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.tear_down_editor)
        deadline = time.monotonic() + 15
        last = ""
        while time.monotonic() < deadline:
            try:
                self.nvim.call("state")
                return
            except EditorError as exc:
                last = str(exc)
                time.sleep(0.1)
        self.fail("Real Neovim startup failed: " + last)

    def tear_down_editor(self) -> None:
        cleanup(self.context, self.directory)
        self.process.communicate(timeout=10)
        self.workspace.cleanup()

    def query(self, expression: str):
        return json.loads(run(self.context, self.nvim.binary, "--server", self.nvim.socket,
                              "--remote-expr", f"json_encode({expression})"))

    def test_modules_and_exact_light_dark_highlights(self) -> None:
        self.assertTrue(self.query("luaeval('MiniAi ~= nil and MiniCompletion ~= nil and MiniStatusline ~= nil')"))
        for dark in (True, False):
            colors = {"dark": dark, "foreground": "#e8defa", "background": "#090516",
                      "surface": "#140d26", "panel": "#211033", "primary": "#00f5ff",
                      "secondary": "#ff4fcb", "accent": "#955cff", "warning": "#ffe66d",
                      "error": "#ff709e", "success": "#00c9d6"}
            if not dark:
                colors.update(foreground="#112233", background="#ffffff")
            self.nvim.call("theme", colors)
            actual = self.query("luaeval('vim.api.nvim_get_hl(0, {name=\"Normal\"})')")
            self.assertEqual(actual["fg"], int(colors["foreground"][1:], 16))
            self.assertEqual(actual["bg"], int(colors["background"][1:], 16))
            self.assertEqual(self.query("&background"), "dark" if dark else "light")
            self.assertEqual(self.query("g:terminal_color_4"), colors["primary"])

    def test_hostile_filename_open_modified_preservation_and_save(self) -> None:
        root = Path(self.workspace.name)
        first = root / "- ' | quit! | <CR>\nfile.py"
        first.write_text("original\n")
        self.nvim.call("open", {"path": str(first)})
        self.assertEqual(self.query("expand('%:p')"), str(first))
        run(self.context, self.nvim.binary, "--server", self.nvim.socket, "--remote-expr",
            "luaeval('vim.api.nvim_buf_set_lines(0, 0, -1, false, {\"edited\"})')")
        second = root / "second-\u03bb.py"
        second.write_text("second\n")
        self.nvim.call("open", {"path": str(second)})
        modified = self.nvim.call("state")["modified"]
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0]["name"], str(first))
        self.nvim.call("save", {"paths": {}})
        self.assertEqual(first.read_text(), "edited\n")
        self.assertFalse(self.nvim.call("state")["modified"])

    def test_unnamed_buffer_save_destination_and_save_error(self) -> None:
        run(self.context, self.nvim.binary, "--server", self.nvim.socket, "--remote-expr",
            "luaeval('vim.api.nvim_buf_set_lines(0, 0, -1, false, {\"new contents\"})')")
        modified = self.nvim.call("state")["modified"]
        self.assertEqual(modified[0]["name"], "")
        with self.assertRaises(EditorError):
            self.nvim.call("save")
        saved = Path(self.workspace.name) / "created.py"
        self.nvim.call("save", {"paths": {str(modified[0]["buffer"]): str(saved)}})
        self.assertEqual(saved.read_text(), "new contents\n")
