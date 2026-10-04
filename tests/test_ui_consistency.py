from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual import events
from textual.geometry import Region
from textual.widgets import Button, Input, Select, Static, TextArea

import test_menu as menus
from synai.models import ChatEvent, Session
from synai.providers.base import ProviderError
from support import save_managed
from synai.tui.application import ApprovalScreen, HistoryScreen
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import (
    ConnectionMenu, ConversationDetails, ConversationMenu, EnvironmentMenu,
    HelpMenu, SandboxSwitch,
)
from synai.tui.navigation import MenuBody, MenuInput


class UIConsistencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            provider = patch(target, menus.MenuProvider)
            provider.start()
            self.addCleanup(provider.stop)

    async def test_every_dashboard_action_is_reachable_with_arrows(self) -> None:
        for size in ((60, 20), (80, 24), (140, 45)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                app = menus.MenuTests().app(Path(directory))
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    await menus.MenuTests().create(pilot, app)
                    await pilot.press("f2")
                    await pilot.pause()
                    screen = app.screen
                    controls = [
                        control for control in screen.focus_chain
                        if isinstance(control, (Button, Select))
                    ]
                    edges: dict[str, set[str]] = {}
                    before = app.session.to_dict()
                    for control in controls:
                        edges[control.id] = set()
                        for direction in ("up", "down", "left", "right"):
                            screen.set_focus(control, scroll_visible=False)
                            control.scroll_visible(animate=False)
                            await pilot.pause(0.01)
                            await pilot.press(direction)
                            edges[control.id].add(screen.focused.id)
                    reached = {"menu-back"}
                    while True:
                        expanded = reached | set().union(*(edges[node] for node in reached))
                        if expanded == reached:
                            break
                        reached = expanded
                    self.assertEqual(reached, {control.id for control in controls})
                    self.assertEqual(app.session.to_dict(), before)

    async def test_field_editing_requires_enter_and_retains_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("alt+o")
                await pilot.pause()
                editor = app.screen
                field = editor.query_one("#connection-url", MenuInput)
                original = field.value
                await pilot.press("x", "backspace", "delete", "ctrl+x")
                field.post_message(events.Paste("not editing"))
                await pilot.pause()
                self.assertEqual(field.value, original)
                await pilot.press("down")
                self.assertEqual(editor.focused.id, "connection-timeout")
                await pilot.press("up", "enter", "a", "b", "c")
                self.assertEqual(field.value, "abc")
                self.assertTrue(field.editing)
                await pilot.press("up", "down")
                self.assertIs(editor.focused, field)
                field.post_message(events.Paste("/draft"))
                await pilot.pause()
                self.assertEqual(field.value, "abc/draft")
                await pilot.press("f1")
                await pilot.pause()
                self.assertIsInstance(app.screen, HelpMenu)
                self.assertTrue(field.editing)
                await pilot.press("escape")
                app.post_message(events.AppBlur())
                await pilot.pause()
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertIs(editor.focused, field)
                self.assertTrue(field.editing)
                await pilot.press("escape")
                self.assertIs(app.screen, editor)
                self.assertFalse(field.editing)
                self.assertEqual(field.value, "abc/draft")
                self.assertEqual(app.settings.ollama_url, original)
                await pilot.press("enter", "z", "enter")
                self.assertEqual(field.value, "z")
                self.assertFalse(field.editing)
                await pilot.press("enter", "tab")
                self.assertFalse(field.editing)
                self.assertEqual(editor.focused.id, "connection-timeout")
                await pilot.press("escape")
                self.assertEqual(app.settings.ollama_url, original)

    async def test_configuration_rail_enters_actual_page_and_returns_without_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(60, 20)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                self.assertEqual(screen.focused.id, "config-nav-overview")
                await pilot.press("down", "down", "down")
                self.assertEqual(screen.focused.id, "config-nav-limits")
                baseline = screen.form_values()
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "env-command_timeout")
                await pilot.press("down")
                self.assertEqual(screen.focused.id, "env-output_bytes")
                await pilot.press("left")
                self.assertEqual(screen.focused.id, "config-nav-limits")
                self.assertEqual(screen.form_values(), baseline)
                self.assertTrue(all(not body.can_focus for body in screen.query(MenuBody)))

    async def test_configuration_paging_uses_active_form_without_moving_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                page = screen.query_one("#config-page-overview", MenuBody)
                rail = screen.query_one("#configuration-nav", MenuBody)
                screen.query_one("#configuration-overview", Static).update("Details\n" * 80)
                await pilot.pause()
                for identifier in ("config-nav-overview", "environment-apply", "environment-back"):
                    screen.query_one(f"#{identifier}", Button).focus()
                    await pilot.pause()
                    page.scroll_to(y=0, animate=False, force=True)
                    await pilot.pause()
                    focused = screen.focused
                    await pilot.press("pagedown")
                    await pilot.pause()
                    self.assertIs(screen.focused, focused)
                    self.assertGreater(page.scroll_y, 0)
                    self.assertEqual(rail.scroll_y, 0)
                    await pilot.press("pageup")
                    await pilot.pause()
                    self.assertEqual(page.scroll_y, 0)

    async def test_resize_preserves_edit_and_dropdown_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.press("alt+o", "enter", "d", "r", "a", "f", "t", "left")
                editor = app.screen
                field = editor.query_one("#connection-url", MenuInput)
                cursor = field.cursor_position
                await pilot.press("f1")
                await pilot.resize_terminal(60, 20)
                await pilot.pause()
                await pilot.press("escape")
                self.assertIs(app.screen, editor)
                self.assertIs(editor.focused, field)
                self.assertTrue(field.editing)
                self.assertEqual(field.value, "draft")
                self.assertEqual(field.cursor_position, cursor)
                await pilot.press("escape", "escape", "alt+n", "down", "right", "enter")
                await pilot.pause()
                selector = app.screen.query_one("#env-execution_mode", Select)
                self.assertTrue(selector.expanded)
                overlay = app.screen.focused
                value = selector.value
                await pilot.resize_terminal(140, 45)
                await pilot.pause()
                self.assertTrue(selector.expanded)
                self.assertIs(app.screen.focused, overlay)
                self.assertEqual(selector.value, value)
                await pilot.press("escape")
                self.assertIs(app.screen.focused, selector)
                await pilot.press("left")
                self.assertEqual(app.screen.focused.id, "config-nav-sandbox")

    async def test_model_discovery_keeps_safe_focus_and_disables_unavailable_dropdown(self) -> None:
        gate = asyncio.Event()

        class DelayedProvider(menus.MenuProvider):
            async def list_models(self):
                await gate.wait()
                raise ProviderError("Offline fixture")

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                environment = app.screen
                with patch("synai.tui.menu.OllamaProvider", DelayedProvider):
                    await menus.click(pilot, app, "#environment-apply")
                    await pilot.pause()
                    screen = app.screen
                    self.assertIsInstance(screen, ConversationMenu)
                    self.assertEqual(screen.focused.id, "conversation-back")
                    selector = screen.query_one("#conversation-choice", Select)
                    self.assertTrue(selector.disabled)
                    await pilot.press("up", "left", "down", "right")
                    self.assertEqual(screen.focused.id, "conversation-back")
                    gate.set()
                    await pilot.pause()
                    self.assertTrue(selector.disabled)
                    self.assertEqual(screen.focused.id, "conversation-back")
                    self.assertIn("Offline fixture", str(screen.query_one("#conversation-message", Static).render()))
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIs(app.screen, environment)
                    self.assertEqual(app.history.list_paths(), [])

    async def test_modal_alignment_initial_focus_and_footer_at_four_sizes(self) -> None:
        for size in ((60, 20), (80, 24), (100, 36), (140, 45)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                long_folder = root / ("long-directory-name-" * 4)
                long_folder.mkdir()
                app = menus.MenuTests().app(root)
                session = Session("model:one", app.settings.ollama_url, directory, title="saved")
                save_managed(app, session)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    environment = EnvironmentMenu(app, new=True)
                    screens = [
                        app.screen, ConnectionMenu(app), environment,
                        ConversationMenu(app, environment, environment=session.environment),
                        ConversationDetails(app), HelpMenu(),
                        DirectoryPicker(root, app.settings), DirectoryPicker(long_folder, app.settings),
                        HistoryScreen(app),
                        ApprovalScreen("Confirm action", "Long details\n" * 80, destructive=True),
                        SandboxSwitch("sandbox", False),
                    ]
                    for screen in screens:
                        with self.subTest(screen=type(screen).__name__):
                            if screen is not app.screen:
                                app.push_screen(screen)
                                await pilot.pause()
                            self.assertIsNotNone(screen.focused)
                            self.assertTrue(screen.focused.can_focus)
                            self.assertFalse(screen.focused.disabled)
                            self.assertNotIsInstance(screen.focused, MenuBody)
                            shell = screen.query_one(".modal-shell, .environment-card, #dashboard")
                            self.assertLessEqual(abs(shell.region.x * 2 + shell.region.width - size[0]), 1)
                            self.assertLessEqual(abs(shell.region.y * 2 + shell.region.height - size[1]), 1)
                            for hint in screen.query(".menu-hints"):
                                self.assertLessEqual(hint.region.bottom, shell.content_region.bottom)
                            for listing in screen.query("#directory-list, #history-selection"):
                                self.assertGreater(listing.content_region.height, 0)
                            for row in screen.query(".modal-actions"):
                                buttons = list(row.query(Button))
                                widths = [button.region.width for button in buttons]
                                self.assertLessEqual(max(widths) - min(widths), 1)
                                for button in buttons:
                                    self.assertGreater(button.content_region.width, 0)
                                    self.assertGreaterEqual(button.region.x, 0)
                                    self.assertLessEqual(button.region.right, size[0])
                                    self.assertLessEqual(button.region.bottom, size[1])
                                    self.assertLessEqual(button.region.bottom, shell.content_region.bottom)
                                    rendered = " ".join(strip.text[1:-1].strip() for strip in button.render_lines(
                                        Region(0, 0, button.region.width, button.region.height),
                                    ))
                                    self.assertIn(str(button.label), rendered)
                                for first, second in zip(buttons, buttons[1:]):
                                    self.assertFalse(first.region.overlaps(second.region))
                            if screen is not screens[0]:
                                app.pop_screen()
                                await pilot.pause()

    async def test_details_is_live_read_only_and_restores_dashboard_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                dashboard = app.screen
                button = dashboard.query_one("#menu-details-open", Button)
                button.focus()
                before = app.session.to_dict()
                await pilot.press("enter")
                await pilot.pause()
                details = app.screen
                self.assertIsInstance(details, ConversationDetails)
                self.assertEqual(details.focused.id, "details-close")
                text = str(details.query_one("#conversation-details", Static).render())
                self.assertIn(app.session.workspace, text)
                self.assertIn(app.session.model, text)
                self.assertIn(str(app.storage.folder(app.session.session_id)), text)
                self.assertEqual(len(details.query(Input)), 0)
                self.assertEqual(len(details.query(Select)), 0)
                await pilot.press("f1", "escape", "escape")
                await pilot.pause()
                self.assertIs(app.screen, dashboard)
                self.assertIs(dashboard.focused, button)
                self.assertEqual(app.session.to_dict(), before)

    async def test_details_during_streaming_does_not_cancel_or_enable_tools(self) -> None:
        started = asyncio.Event()

        class StreamingProvider(menus.MenuProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(content="partial")
                started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                app.provider = app.agent.provider = StreamingProvider()
                app.query_one("#composer", TextArea).load_text("go")
                await pilot.press("ctrl+s")
                await started.wait()
                task = app.turn_task
                await pilot.press("f2")
                await pilot.pause()
                dashboard = app.screen
                dashboard.query_one("#menu-details-open", Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConversationDetails)
                with patch.object(app, "execution_status", return_value="LIVE STATUS CHANGED"):
                    await pilot.pause(0.3)
                    self.assertIn("LIVE STATUS CHANGED", str(
                        app.screen.query_one("#conversation-details", Static).render(),
                    ))
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, dashboard)
                self.assertIs(app.turn_task, task)
                self.assertFalse(task.done())
                self.assertIsNone(app.sandbox.container_id)
                self.assertFalse(app.host.matches(app.session))
                await pilot.press("alt+x")
                await pilot.pause()
                self.assertIsNone(app.turn_task)
