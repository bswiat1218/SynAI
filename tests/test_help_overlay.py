from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Select, Static, TextArea

import test_menu as menus
from synai.models import ChatEvent, Session
from support import save_managed
from synai.tui.application import ApprovalScreen, HistoryScreen
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import ConnectionMenu, ConversationMenu, EnvironmentMenu, HelpMenu, SandboxSwitch


class HelpOverlayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def round_trip(self, pilot, app) -> None:
        owner = app.screen
        focus = owner.focused
        scroll = [(widget, widget.scroll_offset) for widget in owner.query(VerticalScroll)]
        depth = len(app.screen_stack)
        await pilot.press("f1")
        await pilot.pause()
        self.assertIsInstance(app.screen, HelpMenu)
        help_screen = app.screen
        self.assertIn("F1", "\n".join(str(widget.render()) for widget in help_screen.query(Static)))
        self.assertEqual(len(app.screen_stack), depth + 1)
        await pilot.press("f1", "f1", "ctrl+s", "f2")
        await pilot.pause()
        self.assertIs(app.screen, help_screen)
        self.assertEqual(len(app.screen_stack), depth + 1)
        await pilot.press("escape")
        await pilot.pause()
        self.assertIs(app.screen, owner)
        self.assertIs(owner.focused, focus)
        self.assertFalse(app.help_open)
        for widget, offset in scroll:
            self.assertEqual(widget.scroll_offset, offset)

    async def test_dashboard_labels_and_existing_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertEqual(len(screen.query("#menu-help")), 0)
                self.assertEqual(
                    {button.id: str(button.label) for button in screen.query(Button)},
                    {
                        "menu-new": "NEW CONVERSATION", "menu-histories": "CONVERSATIONS",
                        "menu-back": "RETURN TO CHAT", "menu-environment": "SETTINGS",
                        "menu-details-open": "CONVERSATION DETAILS",
                        "menu-editor": "WORKSPACE EDITOR",
                        "disconnect": "DISCONNECT / REMOVE", "menu-stop": "STOP RESPONSE",
                        "menu-connection": "CONNECTION SETTINGS", "menu-refresh": "REFRESH MODELS",
                        "menu-theme": "THEMES", "menu-legacy": "RETRY LEGACY CLEANUP",
                        "menu-quit": "QUIT SynAI",
                    },
                )
                self.assertEqual(screen.query_one("#model", Select).prompt, "Change model")
                await self.round_trip(pilot, app)
                await pilot.press("alt+h")
                await pilot.pause()
                self.assertIsInstance(app.screen, HelpMenu)
                await pilot.press("escape", "alt+n")
                await pilot.pause()
                self.assertIsInstance(app.screen, EnvironmentMenu)
                await pilot.press("escape")
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                self.assertEqual(len(app.screen.query("#menu-help")), 0)
                app.settings = replace(app.settings, execution_mode="host")
                app.screen.update_status()
                self.assertEqual(str(app.screen.query_one("#disconnect", Button).label), "DISCONNECT HOST TOOLS")

    async def test_help_restores_configuration_connection_model_and_picker_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                environment = app.screen
                environment.show_page("limits")
                field = environment.query_one("#env-tool_budget", Input)
                field.value = "7"
                field.focus()
                await pilot.pause()
                values = environment.form_values()
                await self.round_trip(pilot, app)
                self.assertEqual(environment.form_values(), values)
                self.assertEqual(environment.query_one("#configuration-pages").current, "config-page-limits")
                picker = DirectoryPicker(root, app.settings)
                app.push_screen(picker)
                await pilot.pause()
                current = picker.current
                highlighted = picker.query_one("#directory-list").highlighted
                await self.round_trip(pilot, app)
                self.assertEqual(picker.current, current)
                self.assertEqual(picker.query_one("#directory-list").highlighted, highlighted)
                await pilot.press("escape")
                await pilot.pause()
                await menus.click(pilot, app, "#environment-apply")
                self.assertIsInstance(app.screen, ConversationMenu)
                await pilot.pause()
                model = app.screen.query_one("#conversation-choice", Select).value
                await self.round_trip(pilot, app)
                self.assertEqual(app.screen.query_one("#conversation-choice", Select).value, model)
                app.screen.query_one("#conversation-choice", Select).focus()
                await pilot.press("enter")
                await pilot.pause()
                selector = app.screen.query_one("#conversation-choice", Select)
                self.assertTrue(selector.expanded)
                await self.round_trip(pilot, app)
                self.assertTrue(selector.expanded)
                await pilot.press("escape")
                await pilot.press("escape", "escape")
                await pilot.pause()
                await pilot.press("alt+o")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConnectionMenu)
                field = app.screen.query_one(Input)
                field.value = "http://draft:11434"
                field.focus()
                await pilot.press("enter", "end", "left")
                cursor = field.cursor_position
                await self.round_trip(pilot, app)
                self.assertEqual(field.value, "http://draft:11434")
                self.assertEqual(field.cursor_position, cursor)
                self.assertEqual(app.history.list_paths(), [])

    async def test_help_restores_browser_checks_and_chat_composer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            session = Session("model:one", app.settings.ollama_url, directory, title="saved")
            save_managed(app, session)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await pilot.press("alt+c")
                await pilot.pause()
                self.assertIsInstance(app.screen, HistoryScreen)
                listing = app.screen.query_one("#history-selection")
                await pilot.press("space")
                checks, highlighted = list(listing.selected), listing.highlighted
                await self.round_trip(pilot, app)
                self.assertEqual(listing.selected, checks)
                self.assertEqual(listing.highlighted, highlighted)
                await pilot.press("enter")
                await pilot.pause()
                composer = app.query_one("#composer", TextArea)
                composer.load_text("retained draft")
                composer.focus()
                original = app.session.to_dict()
                await self.round_trip(pilot, app)
                self.assertEqual(composer.text, "retained draft")
                self.assertEqual(app.session.to_dict(), original)
                self.assertFalse(app.query_one("#send", Button).disabled)

    async def test_help_during_stream_does_not_cancel_or_send_and_blocks_safety_prompts(self) -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        class SlowProvider(menus.MenuProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(content="first")
                started.set()
                await finish.wait()
                yield ChatEvent(content="last", done=True)

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                app.provider = app.agent.provider = SlowProvider()
                app.query_one("#composer", TextArea).load_text("go")
                await pilot.press("ctrl+s")
                await started.wait()
                task = app.turn_task
                await self.round_trip(pilot, app)
                self.assertIs(app.turn_task, task)
                self.assertFalse(task.done())
                finish.set()
                await task
                self.assertEqual(app.session.messages[-1].content, "firstlast")
                approval = asyncio.create_task(app.approve("Review", "Awaiting decision"))
                await pilot.pause()
                owner = app.screen
                await pilot.press("f1")
                await pilot.pause()
                self.assertIs(app.screen, owner)
                self.assertIsInstance(owner, ApprovalScreen)
                self.assertFalse(approval.done())
                await pilot.press("escape")
                self.assertFalse(await approval)
                app.push_screen(SandboxSwitch("fixture", owned=True))
                await pilot.pause()
                owner = app.screen
                await pilot.press("f1")
                self.assertIs(app.screen, owner)
                await pilot.press("escape")

    async def test_help_waits_for_active_menu_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                owner = app.screen
                owner.applying = True
                await pilot.press("f1")
                await pilot.pause()
                self.assertIs(app.screen, owner)
                self.assertFalse(app.help_open)
                owner.applying = False
                await self.round_trip(pilot, app)
