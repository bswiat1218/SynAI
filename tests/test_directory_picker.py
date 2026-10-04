from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, OptionList, Select, Static

import test_menu as menus
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import EnvironmentMenu


class DirectoryPickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def open_host_picker(self, pilot, app) -> EnvironmentMenu:
        await menus.click(pilot, app, "#menu-new")
        screen = app.screen
        screen.query_one("#env-execution_mode", Select).value = "host"
        await pilot.pause()
        await menus.click(pilot, app, "#config-nav-workspace")
        self.assertTrue(screen.query_one("#env-workspace", Input).disabled)
        await menus.click(pilot, app, "#config-workspace-picker")
        self.assertIsInstance(app.screen, DirectoryPicker)
        return screen

    async def test_keyboard_navigation_selection_updates_only_draft_and_survives_mode_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project [demo]"
            project.mkdir()
            (project / ".hidden").mkdir()
            (root / "not-a-directory.txt").write_text("file")
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                screen = await self.open_host_picker(pilot, app)
                picker = app.screen
                self.assertNotIn("not-a-directory.txt", [p.name for p in picker.directories.values()])
                listing = picker.query_one("#directory-list", OptionList)
                identifier = next(key for key, value in picker.directories.items() if value == project)
                listing.highlighted = int(identifier)
                listing.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(picker.current, project)
                self.assertIn(".hidden", [p.name for p in picker.directories.values()])
                await menus.click(pilot, app, "#directory-choose")
                self.assertIs(app.screen, screen)
                self.assertEqual(screen.query_one("#env-workspace", Input).value, str(project))
                self.assertIn("Unsaved changes", str(screen.query_one("#configuration-draft-status", Static).render()))
                self.assertEqual(app.history.list_paths(), [])
                self.assertFalse(app.storage.folder(screen.conversation_id).exists())
                self.assertIsNone(app.session)
                mode = screen.query_one("#env-execution_mode", Select)
                mode.value = "sandbox"
                await pilot.pause()
                self.assertEqual(screen.query_one("#env-workspace", Input).value, screen.managed_workspace)
                mode.value = "host"
                await pilot.pause()
                self.assertEqual(screen.query_one("#env-workspace", Input).value, str(project))

    async def test_up_home_cancel_and_footer_at_80_by_24(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                screen = await self.open_host_picker(pilot, app)
                original = screen.form_values()
                picker = app.screen
                picker.open_directory(project)
                await pilot.pause()
                await menus.click(pilot, app, "#directory-up")
                self.assertEqual(picker.current, root)
                with patch.object(Path, "home", return_value=root):
                    await menus.click(pilot, app, "#directory-home")
                self.assertEqual(picker.current, root)
                for identifier in ("directory-choose", "directory-cancel"):
                    button = picker.query_one(f"#{identifier}", Button)
                    self.assertGreaterEqual(button.region.y, 0)
                    self.assertLessEqual(button.region.bottom, 24)
                    self.assertLessEqual(button.region.right, 80)
                self.assertGreater(picker.query_one("#directory-list", OptionList).region.height, 1)
                await pilot.press("ctrl+enter")
                self.assertIs(app.screen, picker)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertEqual(screen.form_values(), original)
                await menus.click(pilot, app, "#config-workspace-picker")
                await menus.click(pilot, app, "#directory-cancel")
                self.assertEqual(screen.form_values(), original)

    async def test_errors_and_disallowed_selection_leave_picker_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await self.open_host_picker(pilot, app)
                picker = app.screen
                for forbidden in (Path("/"), Path.home(), app.storage.root, app.storage.conversations):
                    self.assertTrue(picker.open_directory(forbidden))
                    await pilot.pause()
                    await menus.click(pilot, app, "#directory-choose")
                    self.assertIs(app.screen, picker)
                    self.assertTrue(str(picker.query_one("#directory-error", Static).render()))
                picker.open_directory(empty)
                self.assertEqual(picker.directories, {})
                with patch.object(Path, "iterdir", side_effect=PermissionError("Access denied")):
                    self.assertFalse(picker.open_directory(root))
                self.assertEqual(picker.current, empty)
                self.assertIn("Access denied", str(picker.query_one("#directory-error", Static).render()))
                empty.rmdir()
                await pilot.pause(picker.query_one("#directory-choose", Button).active_effect_duration)
                await menus.click(pilot, app, "#directory-choose")
                self.assertIs(app.screen, picker)
                self.assertIn("empty", str(picker.query_one("#directory-error", Static).render()))

    async def test_missing_initial_path_visible_fallback_and_picker_unavailable_when_locked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-new")
                screen = app.screen
                self.assertFalse(screen.query_one("#config-workspace-picker", Button).display)
                screen.query_one("#env-execution_mode", Select).value = "host"
                await pilot.pause()
                screen.query_one("#env-workspace", Input).value = str(root / "missing")
                await menus.click(pilot, app, "#config-nav-workspace")
                with patch.object(Path, "home", return_value=root):
                    await menus.click(pilot, app, "#config-workspace-picker")
                self.assertEqual(app.screen.current, root)
                self.assertIn("Starting directory unavailable", str(app.screen.query_one("#directory-error", Static).render()))
                await pilot.press("escape")
                await pilot.pause()
                screen.applying = True
                screen.refresh_sandbox_controls()
                self.assertTrue(screen.query_one("#config-workspace-picker", Button).disabled)
                screen.applying = False
                screen.sandbox_working = True
                screen.refresh_sandbox_controls()
                self.assertTrue(screen.query_one("#config-workspace-picker", Button).disabled)
                screen.sandbox_working = False
                screen.refresh_sandbox_controls()
