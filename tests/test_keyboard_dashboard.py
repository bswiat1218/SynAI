from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, Select, Static, TextArea

import test_menu as menus
from synai.models import ChatEvent
from synai.tui.application import ApprovalScreen, HistoryScreen
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import ConversationMenu, EnvironmentMenu, MainMenu


async def focus_with_tab(pilot, app, identifier: str) -> None:
    """Reach a control using traversal, never programmatic focus or a mouse."""
    for _ in range(50):
        if app.screen.focused is not None and app.screen.focused.id == identifier:
            return
        await pilot.press("tab")
    raise AssertionError(f"Cannot reach {identifier} with Tab on {type(app.screen).__name__}")


async def activate_with_tab(pilot, app, identifier: str) -> None:
    await focus_with_tab(pilot, app, identifier)
    await pilot.press("enter")
    await pilot.pause()


class KeyboardDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def create_with_keyboard(self, pilot, app) -> None:
        await pilot.press("alt+n")
        await pilot.pause()
        self.assertIsInstance(app.screen, EnvironmentMenu)
        await activate_with_tab(pilot, app, "environment-apply")
        self.assertIsInstance(app.screen, ConversationMenu)
        await activate_with_tab(pilot, app, "conversation-open")
        self.assertNotIsInstance(app.screen, MainMenu)
        self.assertEqual(app.session.model, "model:one")

    async def test_keyboard_only_create_send_panes_model_browser_and_cancel_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await self.create_with_keyboard(pilot, app)
                original = app.session.session_id
                self.assertEqual(len(app.query("#sidebar, #model, #history, #workspace, #image")), 0)
                await pilot.press("H", "i", "enter", "!", "ctrl+s")
                await pilot.pause()
                self.assertEqual(next(message.content for message in app.session.messages if message.role == "user"), "Hi\n!")
                self.assertEqual(app.session.messages[-1].content, "Hello")
                await pilot.press("f3", "d", "r", "a", "f", "t")
                for key, identifier in (("f4", "chat"), ("f5", "thinking"), ("f6", "activity"), ("f3", "composer")):
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertEqual(app.screen.focused.id, identifier)
                    self.assertTrue(app.screen.focused.region.overlaps(app.screen.region))
                    if identifier != "composer":
                        await pilot.press("pageup", "pagedown", "home", "end")
                await pilot.press("f2")
                await pilot.pause()
                dashboard = app.screen
                self.assertIsInstance(dashboard, MainMenu)
                await pilot.press("f2")
                await pilot.pause()
                self.assertEqual(app.screen.focused.id, "composer")
                self.assertEqual(app.query_one("#composer", TextArea).text, "draft")
                await pilot.press("f2", "alt+m", "enter", "end", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ApprovalScreen)
                self.assertEqual(app.screen.focused.id, "deny")
                await pilot.press("shift+tab", "enter")
                await pilot.pause()
                self.assertEqual(app.session.model, "model:two")
                self.assertIn("model:two", str(app.query_one("#conversation-summary", Static).render()))
                await activate_with_tab(pilot, app, "menu-theme")
                self.assertEqual(type(app.screen).__name__, "ThemePicker")
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("alt+c")
                await pilot.pause()
                browser = app.screen
                self.assertIsInstance(browser, HistoryScreen)
                await pilot.press("space")
                self.assertEqual(browser.query_one("#history-selection").selected, [original])
                await activate_with_tab(pilot, app, "delete-selected")
                self.assertIsInstance(app.screen, ApprovalScreen)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, browser)
                self.assertTrue(app.history.path_for(original).exists())
                await focus_with_tab(pilot, app, "history-selection")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.session.session_id, original)
                self.assertNotIsInstance(app.screen, MainMenu)

    async def test_streaming_dashboard_locks_actions_stops_and_restores_focus(self) -> None:
        started = asyncio.Event()

        class StreamingProvider(menus.MenuProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(content="partial", thinking="reasoning")
                started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self.create_with_keyboard(pilot, app)
                provider = StreamingProvider()
                app.provider, app.agent.provider = provider, provider
                await pilot.press("g", "o", "ctrl+enter")
                await started.wait()
                task = app.turn_task
                await pilot.press("f2")
                await pilot.pause()
                dashboard = app.screen
                self.assertIsInstance(dashboard, MainMenu)
                for identifier in ("menu-new", "menu-histories", "menu-environment", "menu-connection",
                                   "menu-refresh", "disconnect", "model"):
                    self.assertTrue(dashboard.query_one(f"#{identifier}").disabled)
                app.theme = "dracula"
                await pilot.pause()
                await pilot.press("alt+n", "alt+c", "alt+s", "alt+m", "alt+r", "alt+d", "ctrl+s", "f3")
                self.assertIs(app.screen, dashboard)
                self.assertIs(app.turn_task, task)
                self.assertEqual(len(app.history.list_paths()), 1)
                await pilot.press("alt+x")
                await pilot.pause()
                self.assertTrue(task.done())
                self.assertIsNone(app.turn_task)
                self.assertEqual(app.session.state, "cancelled")
                self.assertEqual(app.session.messages[-1].content, "partial")
                self.assertFalse(dashboard.query_one("#menu-new", Button).disabled)
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.screen.focused.id, "composer")
                approval = asyncio.create_task(app.approve("Command", "Review before running"))
                await pilot.pause()
                modal = app.screen
                await pilot.press("f2", "f3", "f4", "f5", "f6", "ctrl+s")
                self.assertIs(app.screen, modal)
                self.assertEqual(app.screen.focused.id, "deny")
                await pilot.press("enter")
                self.assertFalse(await approval)

    async def test_keyboard_configuration_validation_host_picker_and_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                await activate_with_tab(pilot, app, "config-nav-limits")
                await focus_with_tab(pilot, app, "env-tool_budget")
                await pilot.press("enter", "home", "shift+end", "0")
                await activate_with_tab(pilot, app, "environment-apply")
                self.assertIsInstance(app.screen, EnvironmentMenu)
                self.assertEqual(app.screen.focused.id, "env-tool_budget")
                await pilot.press("home", "shift+end", "5")
                await activate_with_tab(pilot, app, "config-nav-sandbox")
                await pilot.press("right")
                if app.screen.focused.id != "env-execution_mode":
                    await pilot.press("shift+tab")
                self.assertEqual(app.screen.focused.id, "env-execution_mode")
                await pilot.press("enter", "end", "enter")
                await pilot.pause()
                await activate_with_tab(pilot, app, "config-nav-workspace")
                await pilot.press("right")
                self.assertEqual(app.screen.focused.id, "config-workspace-picker")
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, DirectoryPicker)
                await pilot.press("end", "enter")
                await pilot.pause()
                self.assertEqual(app.screen.current, project)
                await activate_with_tab(pilot, app, "directory-choose")
                self.assertEqual(app.screen.query_one("#env-workspace", Input).value, str(project))
                await activate_with_tab(pilot, app, "environment-apply")
                await activate_with_tab(pilot, app, "conversation-open")
                self.assertIsInstance(app.screen, ApprovalScreen)
                self.assertEqual(app.screen.focused.id, "deny")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.session.workspace, str(project))
                self.assertFalse(app.host.matches(app.session))
                await pilot.press("f2", "alt+d")
                await pilot.pause()
                self.assertIsInstance(app.screen, MainMenu)
                self.assertTrue(project.exists())

    async def test_dashboard_layout_and_all_enabled_controls_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await self.create_with_keyboard(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                for size in ((80, 24), (140, 45), (60, 24)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    screen = app.screen
                    self.assertLessEqual(screen.query_one("#dashboard").region.right, size[0])
                    for identifier in ("menu-new", "menu-histories", "menu-back", "model", "menu-environment",
                                       "disconnect", "menu-connection", "menu-refresh", "menu-theme",
                                       "menu-quit"):
                        await focus_with_tab(pilot, app, identifier)
                        await pilot.pause()
                        control = screen.focused
                        self.assertGreaterEqual(control.region.x, 0)
                        self.assertLessEqual(control.region.right, size[0])
                        self.assertTrue(control.region.overlaps(screen.query_one("#dashboard-content").region))


if __name__ == "__main__":
    unittest.main()
