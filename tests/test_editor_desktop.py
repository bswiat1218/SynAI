from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("SYNAI_TEST_DESKTOP") and os.environ.get("SYNAI_TEST_MINI_PATH"),
                     "Run with system Python under a desktop/Xvfb and SYNAI_TEST_DESKTOP=1")
class DesktopIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        executable = os.environ.get("SYNAI_TEST_WINDOW_MANAGER")
        if executable:
            cls.wm = subprocess.Popen(
                [executable, "--replace", "--compositor=off"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def stop_manager() -> None:
                cls.wm.terminate()
                cls.wm.communicate(timeout=10)
            cls.addClassCleanup(stop_manager)
            time.sleep(0.5)
            if cls.wm.poll() is not None:
                raise RuntimeError(f"Test window manager failed: {cls.wm.communicate()[1]}")

    def setUp(self) -> None:
        from synai.editor.desktop import Desktop, GLib, Gtk
        from synai.editor.environment import EditorContext
        from synai.editor.protocol import COLORS
        self.GLib, self.Gtk = GLib, Gtk
        self.root = tempfile.TemporaryDirectory()
        self.workspace = self.make_workspace()
        self.file = self.workspace / "open me ' safely.py"
        self.file.write_text("original\n")
        self.messages = []
        self.desktop = Desktop()
        self.desktop.send = self.messages.append
        colors = {"dark": True, **{key: "#123456" for key in COLORS}}
        context = self.make_context(EditorContext)
        self.desktop.receive({"type": "launch", "id": 1, "context": context.payload(), "palette": colors})
        self.addCleanup(self.shutdown)
        self.wait(lambda: self.desktop.ready or self.desktop.exited)
        self.assertTrue(self.desktop.ready, self.messages)
        self.assertEqual({entry.name for entry in self.workspace.iterdir()}, {self.file.name},
                         "Editor control/state files must stay outside the workspace")

    def make_workspace(self) -> Path:
        return Path(self.root.name)

    def make_context(self, context_type):
        return context_type("desktop-test", str(self.workspace), "host", uid=os.getuid(),
                            mini_path=os.environ["SYNAI_TEST_MINI_PATH"])

    def wait(self, condition, timeout: float = 25) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            while self.GLib.MainContext.default().pending():
                self.GLib.MainContext.default().iteration(False)
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"Desktop condition timed out: {self.messages}")

    def shutdown(self) -> None:
        if not self.desktop.exited:
            from synai.editor.environment import cleanup
            cleanup(self.desktop.context, self.desktop.directory)
            self.desktop.finish()
        self.root.cleanup()

    def respond(self, response) -> None:
        def find_dialog() -> bool:
            for window in self.Gtk.Window.list_toplevels():
                if isinstance(window, self.Gtk.MessageDialog) and window.get_visible():
                    window.response(response)
                    return False
            return True
        self.GLib.timeout_add(30, find_dialog)

    def test_live_tree_editor_terminal_theme_focus_and_clean_close(self) -> None:
        from synai.editor.environment import run
        desktop = self.desktop
        self.assertIn("HOST" if desktop.context.mode == "host" else "SANDBOX", desktop.banner.get_text())
        self.assertTrue(desktop.window.get_visible())
        self.assertTrue(desktop.editor.get_visible())
        self.assertTrue(desktop.terminal.get_visible())
        if os.environ.get("SYNAI_TEST_WINDOW_MANAGER"):
            from synai.editor.desktop import Gdk
            self.wait(lambda: bool(desktop.window.get_window().get_state() & Gdk.WindowState.MAXIMIZED))
        self.assertGreaterEqual(desktop.tree.get_allocated_width(), 150)
        self.assertGreaterEqual(desktop.terminal.get_allocated_height(), 60)
        editor_y = desktop.editor.translate_coordinates(desktop.window, 0, 0)[1]
        terminal_y = desktop.terminal.translate_coordinates(desktop.window, 0, 0)[1]
        self.assertGreaterEqual(terminal_y, editor_y + desktop.editor.get_allocated_height())
        self.wait(lambda: len(desktop.tree_store) > 0
                  and desktop.tree_store.iter_n_children(desktop.tree_store.get_iter_first()) > 0
                  and desktop.tree_store[desktop.tree_store.iter_children(desktop.tree_store.get_iter_first())][1])
        root = desktop.tree_store.get_iter_first()
        node = desktop.tree_store.iter_children(root)
        while desktop.tree_store[node][1] != str(self.file):
            node = desktop.tree_store.iter_next(node)
            self.assertIsNotNone(node)
        desktop.activate_file(desktop.tree, desktop.tree_store.get_path(node), None)
        self.wait(lambda: run(desktop.context, "nvim", "--server", desktop.nvim.socket,
                              "--remote-expr", "expand('%:p')").strip()
                  == desktop.context.file_path(str(self.file)))
        desktop.terminal.feed_child(b"printf ready > terminal-ready\n")
        self.wait(lambda: (self.workspace / "terminal-ready").exists())
        self.assertEqual((self.workspace / "terminal-ready").read_text(), "ready")
        colors = {**desktop.colors, "dark": False, "foreground": "#112233", "background": "#ffffff"}
        desktop.receive({"type": "theme", "id": 2, "palette": colors})
        self.wait(lambda: any(message.get("id") == 2 for message in self.messages))
        self.assertEqual(desktop.colors, colors)
        self.assertEqual(run(desktop.context, "nvim", "--server", desktop.nvim.socket,
                             "--remote-expr", "&background").strip(), "light")
        desktop.receive({"type": "focus", "id": 3})
        self.assertTrue(any(message.get("id") == 3 for message in self.messages))
        desktop.close(4)
        self.wait(lambda: desktop.exited)
        self.assertTrue(any(message.get("id") == 4 and not message.get("cancelled")
                            for message in self.messages))
        self.assertFalse(Path(desktop.directory).exists())

    def test_modified_close_cancel_then_save(self) -> None:
        from synai.editor.environment import run
        desktop = self.desktop
        desktop.nvim.call("open", {"path": desktop.context.file_path(str(self.file))})
        run(desktop.context, "nvim", "--server", desktop.nvim.socket, "--remote-expr",
            "luaeval('vim.api.nvim_buf_set_lines(0,0,-1,false,{\"saved from GUI\"})')")
        self.respond(self.Gtk.ResponseType.CANCEL)
        desktop.close(5)
        self.wait(lambda: any(message.get("id") == 5 for message in self.messages))
        self.assertFalse(desktop.exited)
        self.assertTrue(desktop.nvim.call("state")["modified"])
        self.respond(self.Gtk.ResponseType.ACCEPT)
        desktop.close(6)
        self.wait(lambda: desktop.exited)
        self.assertEqual(self.file.read_text(), "saved from GUI\n")

    def test_modified_discard_and_terminal_job_confirmation(self) -> None:
        from synai.editor.environment import run
        desktop = self.desktop
        desktop.nvim.call("open", {"path": desktop.context.file_path(str(self.file))})
        run(desktop.context, "nvim", "--server", desktop.nvim.socket, "--remote-expr",
            "luaeval('vim.api.nvim_buf_set_lines(0,0,-1,false,{\"discard me\"})')")
        desktop.terminal.feed_child(b"sleep 120\n")
        self.wait(lambda: bool(desktop.nvim.jobs()))
        responses = [self.Gtk.ResponseType.REJECT, self.Gtk.ResponseType.ACCEPT]
        def confirm() -> bool:
            for window in self.Gtk.Window.list_toplevels():
                if isinstance(window, self.Gtk.MessageDialog) and window.get_visible():
                    window.response(responses.pop(0))
                    return bool(responses)
            return True
        self.GLib.timeout_add(30, confirm)
        desktop.close(7)
        self.wait(lambda: desktop.exited)
        self.assertFalse(responses)
        self.assertEqual(self.file.read_text(), "original\n")

    def test_save_failure_retains_dirty_buffer(self) -> None:
        from synai.editor.environment import run
        desktop = self.desktop
        desktop.nvim.call("open", {"path": desktop.context.file_path(str(self.file))})
        run(desktop.context, "nvim", "--server", desktop.nvim.socket, "--remote-expr",
            "luaeval('vim.api.nvim_buf_set_lines(0,0,-1,false,{\"must not lose\"})')")
        self.file.chmod(0o444)
        self.workspace.chmod(0o500)
        try:
            self.respond(self.Gtk.ResponseType.ACCEPT)
            desktop.close(8)
            self.wait(lambda: any(message.get("id") == 8 for message in self.messages))
            reply = next(message for message in self.messages if message.get("id") == 8)
            self.assertFalse(reply["ok"])
            self.assertFalse(desktop.exited)
            self.assertTrue(desktop.nvim.call("state")["modified"])
            self.assertTrue(desktop.editor.get_sensitive())
        finally:
            self.workspace.chmod(0o700)
            self.file.chmod(0o600)

    def test_parent_disconnect_retains_modified_buffers_on_cancel(self) -> None:
        from synai.editor.environment import run
        desktop = self.desktop
        desktop.nvim.call("open", {"path": desktop.context.file_path(str(self.file))})
        run(desktop.context, "nvim", "--server", desktop.nvim.socket, "--remote-expr",
            "luaeval('vim.api.nvim_buf_set_lines(0,0,-1,false,{\"recover me\"})')")
        self.respond(self.Gtk.ResponseType.CANCEL)
        desktop.parent_eof()
        self.wait(lambda: not desktop.closing)
        self.assertFalse(desktop.exited)
        self.assertIn("SynAI disconnected", desktop.banner.get_text())
        self.assertTrue(desktop.nvim.call("state")["modified"])
        self.respond(self.Gtk.ResponseType.ACCEPT)
        desktop.close(None)
        self.wait(lambda: desktop.exited)
        self.assertEqual(self.file.read_text(), "recover me\n")

    def test_directory_limit_is_exact_and_refresh_recovers(self) -> None:
        for number in range(2000):
            (self.workspace / f"many-{number}").touch()
        desktop = self.desktop
        desktop.refresh()
        self.wait(lambda: "more than 2000 entries" in desktop.status.get_text())
        (self.workspace / "many-1999").unlink()
        desktop.refresh()
        self.wait(lambda: desktop.tree_store.iter_n_children(desktop.tree_store.get_iter_first()) == 2000)
        self.assertEqual(desktop.status.get_text(), "File tree refreshed")


@unittest.skipUnless(os.environ.get("SYNAI_TEST_EDITOR_IMAGE"), "Requires approved test sandbox image")
class SandboxDesktopIntegrationTests(DesktopIntegrationTests):
    def make_workspace(self) -> Path:
        import asyncio
        from dataclasses import replace
        from uuid import uuid4
        from synai.config import Settings
        from synai.sandbox import Sandbox
        from synai.storage import ConversationStorage
        settings = replace(Settings(), history_dir=Path(self.root.name) / "history",
                           image=os.environ["SYNAI_TEST_EDITOR_IMAGE"])
        storage = ConversationStorage(settings.history_dir)
        self.identifier = uuid4().hex
        storage.create(self.identifier, workspace=True)
        workspace = storage.workspace(self.identifier)
        self.sandbox = Sandbox(settings)
        asyncio.run(self.sandbox.create(workspace, settings.image))
        self.addCleanup(lambda: asyncio.run(self.sandbox.remove_owned()))
        return workspace

    def make_context(self, context_type):
        return context_type(
            self.identifier, str(self.workspace), "sandbox", self.sandbox.settings.runtime,
            self.sandbox.container_id, self.sandbox.uid, "/opt/synai/mini.nvim")

    def test_container_loss_reports_failure_without_host_fallback(self) -> None:
        import asyncio
        asyncio.run(self.sandbox.remove_owned())
        self.wait(lambda: self.desktop.exited)
        self.assertEqual(self.desktop.context.mode, "sandbox")
        self.assertTrue(any(message.get("type") == "error" for message in self.messages))


if __name__ == "__main__":
    unittest.main()
