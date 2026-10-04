from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_menu as menus
from synai.config import ConversationEnvironment
from synai.history import HistoryError
from synai.models import Session
from synai.editor.environment import EditorContext, desktop_python
from synai.editor.manager import EditorManager
from synai.editor.neovim import expression
from synai.editor.protocol import COLORS, MAX_MESSAGE, EditorError, decode, encode, palette
from textual.widgets import Button, OptionList, TextArea
from synai.tui.menu import MainMenu


def colors() -> dict[str, object]:
    return {"dark": True, **{name: "#123456" for name in COLORS}}


class EditorContractTests(unittest.TestCase):
    def test_protocol_roundtrip_and_invalid_shapes(self) -> None:
        message = {"type": "theme", "id": 42, "palette": colors()}
        self.assertEqual(decode(encode(message)), {"version": 1, **message})
        for data in (b"[]", b"{}", b"not json", b'{"version":2,"type":"focus"}'):
            with self.assertRaises(EditorError):
                decode(data)
        with self.assertRaises(EditorError):
            encode({"type": "error", "error": "x" * MAX_MESSAGE})
        for invalid in ({}, {"dark": 1}, {**colors(), "primary": "red"}):
            with self.assertRaises(EditorError):
                palette(invalid)
        self.assertEqual(palette(colors()), colors())

    def test_environment_commands_and_workspace_containment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = EditorContext("session-1", directory, "host", uid=1000, mini_path="/plugins")
            host.validate()
            self.assertEqual(EditorContext.from_payload(host.payload()), host)
            self.assertEqual(host.command("/bin/sh", "-i", interactive=True), ["/bin/sh", "-i"])
            sandbox = replace(host, mode="sandbox", runtime="docker", container="abc123")
            self.assertEqual(sandbox.command("nvim", "--version"), [
                "docker", "exec", "-i", "--user", "1000", "--workdir", "/workspace",
                "abc123", "nvim", "--version",
            ])
            self.assertIn("-it", sandbox.command("/bin/sh", "-i", interactive=True))
            name = root / "- weird ' <CR>\nfile.py"
            name.write_text("hello")
            self.assertEqual(sandbox.file_path(str(name)), "/workspace/" + name.name)
            outside = root.parent / "not-inside-editor"
            link = root / "outside"
            link.symlink_to(outside)
            with self.assertRaises((EditorError, OSError)):
                sandbox.file_path(str(link))
            for bad in (replace(sandbox, container="-bad"), replace(host, uid=0),
                        replace(host, shell="relative"), replace(host, mode="invalid")):
                with self.assertRaises(EditorError):
                    bad.validate()

    def test_remote_expression_keeps_values_as_data(self) -> None:
        hostile = "' | quit! | ' <CR>\nfile"
        value = expression("open", {"path": hostile})
        self.assertIn("json_decode('", value)
        self.assertIn("'' | quit! | ''", value)
        self.assertNotIn("\n", value)
        with self.assertRaises(EditorError):
            expression("quit! | malicious", {})

    def test_no_display_has_actionable_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(EditorError, "graphical desktop"):
                desktop_python()


class EditorManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_guard_and_quit_cancel(self) -> None:
        manager = EditorManager(lambda _message, _error: None)
        manager.starting = True
        with self.assertRaisesRegex(ValueError, "Close the F7"):
            manager.guard_context()
        with self.assertRaisesRegex(EditorError, "finish"):
            await manager.close()
        manager.starting = False
        process = AsyncMock()
        process.returncode = None
        manager.process = process
        with patch.object(manager, "request", AsyncMock(return_value={"cancelled": True})):
            self.assertFalse(await manager.close())
            process.wait.assert_not_awaited()
        with patch.object(manager, "request", AsyncMock(return_value={"cancelled": False})):
            self.assertTrue(await manager.close())
            process.wait.assert_awaited_once()

    async def test_theme_requests_and_startup_error(self) -> None:
        manager = EditorManager(lambda _message, _error: None)
        process = AsyncMock()
        process.returncode = None
        manager.process = process
        with patch.object(manager, "request", AsyncMock()) as request:
            await manager.set_theme(colors())
            request.assert_awaited_once_with("theme", palette=colors())
        manager.process = None
        with tempfile.TemporaryDirectory() as directory:
            context = EditorContext("session", directory, "host", uid=1000, mini_path="/plugins")
            with patch("synai.editor.manager.desktop_python", side_effect=EditorError("no GTK")):
                with self.assertRaisesRegex(EditorError, "no GTK"):
                    await manager.launch(context, colors())
            self.assertFalse(manager.active)


@unittest.skipUnless(os.environ.get("SYNAI_TEST_DESKTOP") and os.environ.get("SYNAI_TEST_MINI_PATH"),
                     "Run under a desktop/Xvfb with installed test mini.nvim")
class EditorChildIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_child_handshake_focus_theme_and_close(self) -> None:
        reports = []
        manager = EditorManager(lambda message, error: reports.append((message, error)))
        with tempfile.TemporaryDirectory() as directory:
            context = EditorContext(
                "child-test", directory, "host", uid=os.getuid(),
                mini_path=os.environ["SYNAI_TEST_MINI_PATH"])
            try:
                await manager.launch(context, colors())
                self.assertTrue(manager.active)
                self.assertTrue(manager.recovery_directory)
                pid = manager.process.pid
                await manager.launch(context, colors())
                self.assertEqual(manager.process.pid, pid)
                await manager.set_theme({**colors(), "dark": False})
                self.assertTrue(await manager.close())
                self.assertFalse(manager.active)
                self.assertFalse(Path(manager.recovery_directory).exists())
            finally:
                if manager.active:
                    await manager.abort_startup()
                await manager.parent_shutdown()

    async def test_missing_plugin_startup_is_reported_and_child_exits(self) -> None:
        manager = EditorManager(lambda _message, _error: None)
        with tempfile.TemporaryDirectory() as directory:
            context = EditorContext("child-test", directory, "host", uid=os.getuid(),
                                    mini_path=directory)
            with self.assertRaisesRegex(EditorError, "mini.nvim"):
                await manager.launch(context, colors())
            self.assertFalse(manager.active)
            await manager.parent_shutdown()

    async def test_parent_disconnect_closes_clean_child_without_hanging(self) -> None:
        manager = EditorManager(lambda _message, _error: None)
        with tempfile.TemporaryDirectory() as directory:
            context = EditorContext(
                "child-test", directory, "host", uid=os.getuid(),
                mini_path=os.environ["SYNAI_TEST_MINI_PATH"])
            await manager.launch(context, colors())
            await manager.parent_shutdown()
            await asyncio.wait_for(manager.process.wait(), 15)
            self.assertFalse(Path(manager.recovery_directory).exists())


class EditorApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_launch_requires_conversation_and_matching_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            app.approve = AsyncMock(return_value=True)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("f7")
                await pilot.pause()
                self.assertIn("Create or open", app.transient_notes[-1][0])
                await menus.MenuTests().create(pilot, app)
                app.query_one("#composer", TextArea).load_text("keep my draft")
                with patch.object(app.editor, "launch", AsyncMock()) as launch:
                    await pilot.press("f7")
                    await pilot.pause()
                    launch.assert_not_awaited()
                    self.assertIn("validated sandbox", app.transient_notes[-1][0])
                    self.assertEqual(app.query_one("#composer", TextArea).text, "keep my draft")

    async def test_host_shortcut_menu_and_live_palette(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            app.approve = AsyncMock(return_value=True)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                workspace = root / "project"
                workspace.mkdir()
                environment = ConversationEnvironment.from_settings(
                    replace(app.settings, execution_mode="host"), workspace)
                self.assertTrue(await app.create_session("model:one", environment))
                with patch.object(app.editor, "launch", AsyncMock()) as launch:
                    await pilot.press("f7")
                    await pilot.pause()
                    self.assertEqual(launch.await_count, 1)
                    context, initial = launch.call_args.args
                    self.assertEqual(context.mode, "host")
                    self.assertEqual(context.workspace, str(workspace))
                    self.assertEqual(palette(initial), initial)
                    if not isinstance(app.screen, MainMenu):
                        await pilot.press("f2")
                    await pilot.pause()
                    app.screen.query_one("#menu-editor", Button).press()
                    await pilot.pause()
                    self.assertEqual(launch.await_count, 2)
                app.editor.starting = True
                with patch.object(app.editor, "set_theme", AsyncMock()) as theme:
                    app.theme = "textual-light"
                    await pilot.pause()
                    self.assertFalse(theme.call_args.args[0]["dark"])
                app.editor.starting = False

    async def test_context_guards_cover_underlying_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            app.approve = AsyncMock(return_value=True)
            async with app.run_test() as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                original = app.session
                app.editor.starting = True
                for operation in (
                    app.switch_session(original),
                    app.activate_environment(original.environment),
                    app.apply_environment(app.settings, Path(original.workspace)),
                    app.handoff_sandbox(),
                ):
                    with self.assertRaisesRegex(ValueError, "Close the F7"):
                        await operation
                self.assertFalse(await app.create_session("model:one"))
                self.assertIs(app.session, original)
                with self.assertRaisesRegex(ValueError, "Close the F7"):
                    await app.setup_sandbox("disconnect")
                app.editor.context = EditorContext(
                    original.session_id, original.workspace, "sandbox", "docker", "abc",
                    1000, "/opt/synai/mini.nvim")
                with self.assertRaisesRegex(HistoryError, "Close the workspace editor"):
                    await app.check_workspace_unused(original.session_id, app.history.path_for(original.session_id))
                app.editor.starting = False

    async def test_quit_cancel_keeps_application_and_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch.object(app.editor, "close", AsyncMock(return_value=False)), \
                     patch.object(app.host, "revoke") as revoke:
                    await app.action_quit_agent()
                    self.assertTrue(app.is_running)
                    revoke.assert_not_called()
                    self.assertIn("Quit cancelled", app.transient_notes[-1][0])

    async def test_preview_cancel_and_commit_sync_active_palette(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test() as pilot:
                await pilot.pause()
                process = AsyncMock()
                process.returncode = None
                app.editor.process = process
                original = app.editor_palette()
                with patch.object(app.editor, "set_theme", AsyncMock()) as theme:
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    listing = app.screen.query_one("#theme-list", OptionList)
                    listing.highlighted = listing.get_option_index("textual-light")
                    await pilot.pause()
                    self.assertFalse(theme.call_args.args[0]["dark"])
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual(theme.call_args.args[0], original)
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    listing = app.screen.query_one("#theme-list", OptionList)
                    listing.highlighted = listing.get_option_index("dracula")
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(app.preferences.theme, "dracula")
                    self.assertEqual(theme.call_args.args[0], app.editor_palette())
                app.editor.process = None
