from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from textual import events
from textual.color import Color
from textual.geometry import Region
from textual.widgets import Button, OptionList, RichLog, Static, TextArea

import test_menu as menus
from synai.models import ChatEvent
from synai.preferences import DEFAULT_THEME
from synai.tui.menu import ConnectionMenu, HelpMenu, SandboxSwitch
from synai.tui.navigation import MenuInput
from synai.tui.theme_picker import ThemePicker


class ThemePickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            provider = patch(target, menus.MenuProvider)
            provider.start()
            self.addCleanup(provider.stop)

    async def choose(self, pilot, app, name: str) -> None:
        listing = app.screen.query_one("#theme-list", OptionList)
        listing.highlighted = listing.get_option_index(name)
        await pilot.pause()
        self.assertEqual(app.theme, name)

    async def test_arrow_preview_and_rapid_commit_save_only_confirmed_theme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch.object(app.preferences_store, "save", wraps=app.preferences_store.save) as save:
                    await pilot.press("ctrl+p", "down")
                    await pilot.pause()
                    picker = app.screen
                    self.assertIsInstance(picker, ThemePicker)
                    self.assertNotEqual(app.theme, DEFAULT_THEME)
                    save.assert_not_called()
                    await pilot.press("down", "up", "enter")
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, ThemePicker)
                    self.assertEqual(save.call_count, 1)
                    self.assertEqual(app.preferences_store.load().theme, app.theme)
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    listing = app.screen.query_one("#theme-list", OptionList)
                    listing.highlighted = listing.get_option_index("dracula")
                    app.screen.selected = "dracula"
                    app.theme = "dracula"
                    app.screen.apply_selection()
                    await pilot.pause()
                    self.assertEqual(save.call_count, 2)
                    self.assertEqual(app.preferences_store.load().theme, "dracula")

    async def test_preview_cancel_and_commit_have_exact_write_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            app.preferences_store.save(app.preferences)
            original_bytes = app.preferences_store.path.read_bytes()
            async with app.run_test() as pilot:
                await pilot.pause()
                owner = app.screen
                focus = owner.focused
                with patch.object(app.preferences_store, "save", wraps=app.preferences_store.save) as save:
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    picker = app.screen
                    self.assertIsInstance(picker, ThemePicker)
                    depth = len(app.screen_stack)
                    await pilot.press("ctrl+p", "ctrl+p")
                    self.assertIs(app.screen, picker)
                    self.assertEqual(len(app.screen_stack), depth)
                    for name in ("dracula", "textual-light", DEFAULT_THEME, "dracula"):
                        await self.choose(pilot, app, name)
                        self.assertEqual(app.preferences_store.path.read_bytes(), original_bytes)
                    save.assert_not_called()
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIs(app.screen, owner)
                    self.assertIs(owner.focused, focus)
                    self.assertEqual(app.theme, DEFAULT_THEME)
                    self.assertIsNone(app.theme_preview_original)
                    save.assert_not_called()
                    owner.query_one("#menu-theme", Button).focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    await self.choose(pilot, app, "textual-light")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIs(app.screen, owner)
                    self.assertEqual(app.preferences_store.load().theme, "textual-light")
                    self.assertEqual(save.call_count, 1)
                    app.theme = "dracula"
                    await pilot.pause()
                    self.assertEqual(save.call_count, 2)
            restarted = menus.MenuTests().app(Path(directory))
            self.assertEqual(restarted.theme, "dracula")
            await restarted.provider.close()

    async def test_save_failure_retry_and_cancel_preserve_unsaved_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            app.preferences_store.save(app.preferences)
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch.object(app.preferences_store, "save", side_effect=OSError("disk full")):
                    app.theme = "dracula"
                    await pilot.pause()
                self.assertEqual(app.preferences.theme, DEFAULT_THEME)
                await pilot.press("ctrl+p")
                await pilot.pause()
                picker = app.screen
                await self.choose(pilot, app, "textual-light")
                with patch.object(app.preferences_store, "save", side_effect=OSError("disk full")) as save:
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIs(app.screen, picker)
                    self.assertIn("disk full", str(picker.query_one("#theme-error", Static).render()))
                    self.assertEqual(str(picker.query_one("#theme-apply", Button).label), "RETRY")
                    self.assertEqual(save.call_count, 1)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual(app.theme, "dracula")
                    self.assertEqual(app.preferences_store.load().theme, DEFAULT_THEME)
                    self.assertEqual(save.call_count, 1)
                await pilot.press("ctrl+p")
                await pilot.pause()
                await self.choose(pilot, app, "textual-light")
                with patch.object(app.preferences_store, "save", side_effect=OSError("disk full")):
                    await pilot.press("enter")
                    await pilot.pause()
                await pilot.press("tab", "enter")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ThemePicker)
                self.assertEqual(app.preferences_store.load().theme, "textual-light")

    async def test_help_resize_refocus_restore_field_editing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.press("alt+o", "enter", "d", "r", "a", "f", "t", "left")
                owner = app.screen
                self.assertIsInstance(owner, ConnectionMenu)
                field = owner.query_one("#connection-url", MenuInput)
                cursor = field.cursor_position
                await pilot.press("ctrl+p")
                await pilot.pause()
                picker = app.screen
                await self.choose(pilot, app, "textual-light")
                await pilot.press("f1")
                await pilot.pause()
                self.assertIsInstance(app.screen, HelpMenu)
                await pilot.resize_terminal(60, 20)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, picker)
                app.post_message(events.AppBlur())
                await pilot.pause()
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertIs(picker.focused, picker.query_one("#theme-list"))
                picker.set_focus(None)
                await pilot.press("enter")
                self.assertIs(app.screen, picker)
                self.assertEqual(app.preferences.theme, DEFAULT_THEME)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, owner)
                self.assertIs(owner.focused, field)
                self.assertTrue(field.editing)
                self.assertEqual(field.value, "draft")
                self.assertEqual(field.cursor_position, cursor)

    async def test_rapid_preview_cancel_and_unexpected_unmount_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test() as pilot:
                await pilot.pause()
                for dismiss in ("escape", "pop"):
                    with patch.object(app.preferences_store, "save") as save:
                        await pilot.press("ctrl+p")
                        await pilot.pause()
                        app.theme = "dracula"
                        app.theme = "textual-light"
                        if dismiss == "escape":
                            app.screen.action_cancel()
                        else:
                            app.pop_screen()
                        await pilot.pause()
                        await pilot.pause()
                        self.assertEqual(app.theme, DEFAULT_THEME)
                        self.assertFalse(app.theme_picker_open)
                        save.assert_not_called()

    async def test_preview_updates_rendered_content_and_preserves_stream(self) -> None:
        started = asyncio.Event()

        class StreamingProvider(menus.MenuProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(content="streamed answer", thinking="streamed reasoning")
                started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                app.provider = app.agent.provider = StreamingProvider()
                app.query_one("#composer", TextArea).load_text("go")
                await pilot.press("ctrl+s")
                await started.wait()
                app.query_one("#composer", TextArea).load_text("unsent draft")
                task = app.turn_task
                raw = app.session.to_dict()
                original_colors = app.query_one("#chat", RichLog).styles.background
                await pilot.press("ctrl+p")
                await pilot.pause()
                await self.choose(pilot, app, "textual-light")
                self.assertNotEqual(app.query_one("#chat", RichLog).styles.background, original_colors)
                self.assertEqual(app.query_one("#composer", TextArea).text, "unsent draft")
                self.assertEqual(app.session.to_dict(), raw)
                await pilot.press("ctrl+s", "f2", "escape")
                await pilot.pause()
                self.assertIs(app.turn_task, task)
                self.assertFalse(task.done())
                self.assertEqual(app.query_one("#chat", RichLog).styles.background, original_colors)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsNone(app.turn_task)

    async def test_safety_and_menu_transition_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test() as pilot:
                await pilot.pause()
                task = asyncio.create_task(app.approve("Review", "Must explicitly confirm"))
                await pilot.pause()
                screen = app.screen
                await pilot.press("ctrl+p")
                self.assertIs(app.screen, screen)
                self.assertFalse(task.done())
                await pilot.press("escape")
                self.assertFalse(await task)
                app.push_screen(SandboxSwitch("fixture", True))
                await pilot.pause()
                await pilot.press("ctrl+p")
                self.assertIsInstance(app.screen, SandboxSwitch)
                await pilot.press("escape", "alt+o")
                await pilot.pause()
                owner = app.screen
                owner.saving = True
                await pilot.press("ctrl+p")
                self.assertIs(app.screen, owner)
                owner.saving = False

    async def test_layout_theme_foreground_and_saved_connection_at_four_sizes(self) -> None:
        for size in ((60, 20), (80, 24), (100, 36), (140, 45)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                app = menus.MenuTests().app(Path(directory))
                app.preferences = replace(app.preferences, ollama_url="http://saved:11434", request_timeout=55)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    screen = app.screen
                    listing = screen.query_one("#theme-list", OptionList)
                    self.assertGreater(listing.content_region.height, 0)
                    for theme in (DEFAULT_THEME, "textual-light", "dracula"):
                        await self.choose(pilot, app, theme)
                        foreground = Color.parse(app.get_css_variables()["foreground"])
                        for widget in screen.query("Label, Static, Button, OptionList"):
                            self.assertEqual(widget.styles.color, foreground)
                        highlighted = listing.get_component_styles("option-list--option-highlighted")
                        self.assertFalse(highlighted.text_style.underline)
                    for button in screen.query(Button):
                        self.assertLessEqual(button.region.bottom, size[1])
                        rendered = " ".join(strip.text for strip in button.render_lines(
                            Region(0, 0, button.region.width, button.region.height),
                        ))
                        self.assertIn(str(button.label), rendered)
                    await pilot.press("enter")
                    await pilot.pause()
                    saved = app.preferences_store.load()
                    self.assertEqual(saved.ollama_url, "http://saved:11434")
                    self.assertEqual(saved.request_timeout, 55)
                    self.assertEqual(saved.theme, "dracula")
