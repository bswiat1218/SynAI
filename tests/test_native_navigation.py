from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual import events
from textual.widgets import Button, Input, OptionList, Select

import test_menu as menus
from synai.tui.menu import SandboxSwitch
from synai.tui.directory_picker import DirectoryPicker


class NativeNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_pages_open_on_arrow_tab_and_mouse_focus_preserving_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                screen.query_one("#config-nav-overview", Button).focus()
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(screen.query_one("#configuration-pages").current, "config-page-sandbox")
                screen.query_one("#env-tool_budget", Input).value = "7"
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(screen.query_one("#configuration-pages").current, "config-page-workspace")
                await pilot.click("#config-nav-limits")
                await pilot.pause()
                self.assertEqual(screen.query_one("#configuration-pages").current, "config-page-limits")
                self.assertEqual(screen.query_one("#env-tool_budget", Input).value, "7")

    async def test_dropdown_enter_open_focus_commit_and_escape_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                screen.query_one("#config-nav-sandbox", Button).focus()
                await pilot.pause()
                selector = screen.query_one("#env-execution_mode", Select)
                await pilot.press("right")
                await pilot.pause()
                self.assertIs(screen.focused, selector)
                await pilot.press("space")
                self.assertFalse(selector.expanded)
                await pilot.press("down")
                self.assertFalse(selector.expanded)
                self.assertIsNot(screen.focused, selector)
                selector.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(selector.expanded)
                overlay = selector.query_one("SelectOverlay")
                self.assertIs(screen.focused, overlay)
                await pilot.press("end")
                self.assertEqual(selector.value, "sandbox")
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(selector.value, "sandbox")
                self.assertIs(screen.focused, selector)
                await pilot.press("enter", "end", "enter")
                await pilot.pause()
                self.assertEqual(selector.value, "host")
                self.assertIs(screen.focused, selector)
                self.assertFalse(selector.expanded)
                self.assertEqual(app.history.list_paths(), [])

    async def test_refocus_and_first_arrow_recover_without_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                screen = app.screen
                await pilot.press("down")
                app.post_message(events.AppBlur())
                await pilot.pause()
                self.assertIsNone(screen.focused)
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertEqual(screen.focused.id, "menu-histories")
                await pilot.press("up")
                self.assertEqual(screen.focused.id, "menu-new")
                await pilot.pause()
                screen.set_focus(None)
                await pilot.press("down")
                self.assertEqual(screen.focused.id, "menu-histories")
                await pilot.pause()
                screen.query_one("#menu-histories", Button).disabled = True
                screen.set_focus(None)
                with patch.object(screen, "update_status"):
                    await pilot.press("enter")
                    self.assertEqual(screen.focused.id, "menu-new")
                    self.assertEqual(app.history.list_paths(), [])

    async def test_refocus_preserves_open_dropdown_and_safety_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                screen.query_one("#config-nav-sandbox", Button).focus()
                await pilot.pause()
                selector = screen.query_one("#env-execution_mode", Select)
                selector.focus()
                await pilot.press("enter", "end")
                await pilot.pause()
                overlay = screen.focused
                highlight = overlay.highlighted
                app.post_message(events.AppBlur())
                await pilot.pause()
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertIs(screen.focused, overlay)
                self.assertTrue(selector.expanded)
                self.assertEqual(overlay.highlighted, highlight)
                await pilot.press("up")
                self.assertNotEqual(overlay.highlighted, highlight)
                await pilot.press("escape")

    async def test_all_dropdowns_and_refocus_safety(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                dashboard = app.screen

                async def check_selector(selector: Select) -> None:
                    selector.focus()
                    await pilot.pause()
                    await pilot.press("down")
                    self.assertFalse(selector.expanded)
                    selector.focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    overlay = selector.query_one("SelectOverlay")
                    self.assertIs(app.screen.focused, overlay)
                    value = selector.value
                    await pilot.press("home")
                    await pilot.pause()
                    await pilot.press("down")
                    await pilot.pause()
                    self.assertIs(app.screen.focused, overlay)
                    self.assertEqual(selector.value, value)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIs(app.screen.focused, selector)
                    self.assertEqual(selector.value, value)

                await check_selector(dashboard.query_one("#model", Select))
                await pilot.press("alt+s")
                await pilot.pause()
                environment = app.screen
                environment.query_one("#config-nav-sandbox", Button).focus()
                await pilot.pause()
                for identifier in ("env-execution_mode", "env-runtime"):
                    await check_selector(environment.query_one(f"#{identifier}", Select))
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                await menus.click(pilot, app, "#environment-apply")
                await check_selector(app.screen.query_one("#conversation-choice", Select))
                approval = asyncio.create_task(app.approve("Review", "Do not auto-allow"))
                await pilot.pause()
                owner = app.screen
                app.post_message(events.AppBlur())
                await pilot.pause()
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertEqual(owner.focused.id, "deny")
                owner.set_focus(None)
                await pilot.press("enter")
                self.assertFalse(approval.done())
                self.assertEqual(owner.focused.id, "deny")
                await pilot.press("escape")
                self.assertFalse(await approval)
                app.post_message(events.AppBlur())
                await pilot.pause()
                app.push_screen(SandboxSwitch("fixture", owned=True))
                await pilot.pause()
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertEqual(app.screen.focused.id, "switch-cancel")
                await pilot.press("escape")
                await pilot.pause()
                root = Path(directory)
                (root / "a").mkdir()
                (root / "b").mkdir()
                app.push_screen(DirectoryPicker(root, app.settings))
                await pilot.pause()
                listing = app.screen.query_one(OptionList)
                await pilot.press("home")
                await pilot.pause()
                app.screen.set_focus(None)
                await pilot.press("down")
                await pilot.pause()
                self.assertIs(app.screen.focused, listing)
                self.assertEqual(listing.highlighted, 1)
