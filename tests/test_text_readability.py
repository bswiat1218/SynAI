from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from rich.text import Text
from textual.color import Color
from textual.widgets import Button, Input, RichLog, Select, Static, TextArea

import test_menu as menus
from synai.models import Activity, Message
from synai.preferences import DEFAULT_THEME
from synai.tui.activity import format_activity
from synai.tui.application import ApprovalScreen, HistoryScreen
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import ConnectionMenu, EnvironmentMenu, HelpMenu
from synai.tui.palette import CYBERPUNK, rich_theme


THEMES = (DEFAULT_THEME, "textual-light", "dracula", "solarized-dark")


def rendered_colors(widget: Static | RichLog) -> set[Color]:
    lines = widget.lines if isinstance(widget, RichLog) else (
        widget.render_line(y) for y in range(widget.size.height)
    )
    return {
        Color.from_rich_color(segment.style.color)
        for line in lines for segment in line
        if segment.text.strip() and segment.style and segment.style.color
    }


class RichReadabilityTests(unittest.TestCase):
    def test_roles_and_activity_details_use_normal_foreground_without_dim(self) -> None:
        variables = CYBERPUNK.to_color_system().generate()
        console = Console(theme=rich_theme(variables), force_terminal=True, color_system="truecolor")
        expected = Color.parse(variables["foreground"])
        for name in ("text", "primary", "secondary", "accent", "warning", "error", "muted"):
            style = console.get_style(f"synai.{name}")
            self.assertEqual(Color.from_rich_color(style.color), expected)
            self.assertFalse(style.dim)
        entry = Activity("result", 'terminal: {"ok": false, "stdout": "output", "stderr": "error", "exit_code": 1}')
        before = entry.text
        themed = format_activity(entry, themed=True)
        self.assertEqual(themed.plain, format_activity(entry).plain)
        for segment in console.render(themed):
            if segment.text.strip():
                self.assertEqual(Color.from_rich_color(segment.style.color), expected)
                self.assertFalse(segment.style.dim)
        self.assertEqual(entry.text, before)


class TextReadabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    def assert_foreground(self, app, widget) -> None:
        expected = Color.parse(app.get_css_variables()["foreground"])
        self.assertEqual(widget.styles.color, expected, str(widget))
        if isinstance(widget, (Static, RichLog)):
            colors = rendered_colors(widget)
            if colors:
                self.assertEqual(colors, {expected}, str(widget))
            lines = widget.lines if isinstance(widget, RichLog) else (
                widget.render_line(y) for y in range(widget.size.height)
            )
            for line in lines:
                for segment in line:
                    if segment.text.strip() and segment.style:
                        self.assertFalse(segment.style.dim, str(widget))
        self.assertEqual(widget.styles.opacity, 1, str(widget))
        self.assertEqual(widget.styles.text_opacity, 1, str(widget))

    def assert_components(self, app, widget, names) -> None:
        expected = Color.parse(app.get_css_variables()["foreground"])
        for name in names:
            style = widget.get_component_rich_style(name)
            self.assertEqual(Color.from_rich_color(style.color), expected, f"{widget}: {name}")
            self.assertFalse(style.dim, f"{widget}: {name}")
            if name == "option-list--option-highlighted":
                self.assertFalse(style.underline, str(widget))

    async def test_every_button_variant_and_active_page_has_distinct_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()

                async def assert_focus(button: Button, previous: Button) -> None:
                    button.focus()
                    await pilot.pause()
                    variables = app.get_css_variables()
                    self.assertTrue(button.has_focus)
                    self.assertFalse(previous.has_focus)
                    self.assertFalse(previous.styles.text_style.underline)
                    self.assertFalse(button.styles.text_style.underline)
                    self.assertEqual(button.styles.background, Color.parse(variables["surface"]))
                    for edge in button.styles.border:
                        self.assertEqual(edge, ("double", Color.parse(variables["primary"])))
                    self.assert_foreground(app, button)
                    self.assertFalse(any(
                        segment.text.strip() and segment.style and segment.style.underline
                        for y in range(button.size.height) for segment in button.render_line(y)
                    ), button.id)

                for theme in THEMES:
                    with self.subTest(theme=theme):
                        app.theme = theme
                        await pilot.pause()
                        screen = app.screen
                        previous = screen.query_one("#menu-theme", Button)
                        previous.focus()
                        for identifier in ("menu-new", "menu-histories", "menu-quit", "menu-theme"):
                            button = screen.query_one(f"#{identifier}", Button)
                            await assert_focus(button, previous)
                            previous = button
                        quit_button = screen.query_one("#menu-quit", Button)
                        await assert_focus(quit_button, previous)
                        await pilot.hover("#menu-quit")
                        await pilot.pause()
                        await assert_focus(quit_button, previous)
                        quit_button.add_class("-active")
                        await assert_focus(quit_button, previous)
                        quit_button.remove_class("-active")
                        screen.action_activate("menu-new")
                        await pilot.pause()
                        environment = app.screen
                        active = environment.query_one("#config-nav-overview", Button)
                        previous = environment.query_one("#config-nav-workspace", Button)
                        previous.focus()
                        await assert_focus(active, previous)
                        await pilot.press("down")
                        await pilot.pause()
                        execution = environment.query_one("#config-nav-sandbox", Button)
                        self.assertIs(environment.focused, execution)
                        await assert_focus(execution, active)
                        self.assertFalse(active.has_class("active-page"))
                        self.assertTrue(execution.has_class("active-page"))
                        self.assertFalse(active.styles.text_style.underline)
                        environment.query_one("#environment-back", Button).focus()
                        await pilot.pause()
                        self.assertEqual(execution.styles.border.top[0], "solid")
                        await pilot.press("escape")
                        await pilot.pause()
                        approval = asyncio.create_task(app.approve("Review", "Details"))
                        await pilot.pause()
                        deny = app.screen.query_one("#deny", Button)
                        await pilot.press("left")
                        allow = app.screen.query_one("#allow", Button)
                        await assert_focus(allow, deny)
                        await pilot.press("escape")
                        self.assertFalse(await approval)

    async def test_text_controls_states_and_retained_content_across_themes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                app.session.messages = [Message("user", "User text"),
                                        Message("assistant", "Answer text", thinking="Reasoning text")]
                app.session.activity = [Activity("result", 'terminal: {"ok": false, "stderr": "Failed test"}')]
                raw = app.session.to_dict()
                app.render_dirty = True
                app.flush_render()
                for theme in THEMES:
                    with self.subTest(theme=theme):
                        app.theme = theme
                        await pilot.pause()
                        variables = app.get_css_variables()
                        for widget in app.screen.query("Label, Static, Button, Input, SelectCurrent, RichLog, TextArea"):
                            self.assert_foreground(app, widget)
                        for identifier in ("chat", "thinking", "activity"):
                            log = app.query_one(f"#{identifier}", RichLog)
                            self.assertTrue(rendered_colors(log))
                            self.assertEqual(log.styles.background, Color.parse(variables["surface"]))
                        self.assertEqual(app.session.to_dict(), raw)
                        app.note("Warning: retained note", True)
                        self.assert_foreground(app, app.query_one("#status", Static))
                        self.assert_foreground(app, app.query_one("#activity", RichLog))
                        composer = app.query_one("#composer", TextArea)
                        composer.load_text("select this")
                        composer.focus()
                        await pilot.press("f8")
                        self.assert_components(app, composer, (
                            "text-area--cursor", "text-area--selection", "text-area--placeholder",
                            "text-area--suggestion", "text-area--gutter", "text-area--cursor-gutter",
                        ))
                        self.assertEqual(composer.selected_text, "select this")
                        self.assertEqual(composer._theme.base_style.color, Color.parse(variables["foreground"]).rich_color)
                        self.assertEqual(composer._theme.selection_style.color, Color.parse(variables["foreground"]).rich_color)
                        self.assertEqual(composer._theme.cursor_style.color, Color.parse(variables["foreground"]).rich_color)
                        self.assertEqual(composer._theme.base_style.bgcolor, Color.parse(variables["surface"]).rich_color)
                        stop = app.query_one("#stop", Button)
                        self.assertTrue(stop.disabled)
                        self.assertEqual(stop.styles.border.top[0], "dashed")
                        self.assert_foreground(app, stop)
                        send = app.query_one("#send", Button)
                        send.focus()
                        await pilot.pause()
                        self.assert_foreground(app, send)
                        await pilot.hover("#send")
                        await pilot.pause()
                        self.assert_foreground(app, send)
                        self.assertIn(send.styles.background, {
                            Color.parse(variables["panel"]), Color.parse(variables["surface"]),
                        })
                        for footer in app.screen.query("FooterKey"):
                            self.assert_components(app, footer, ("footer-key--key", "footer-key--description"))
                        await pilot.press("f2")
                        await pilot.pause()
                        selector = app.screen.query_one("#model", Select)
                        selector.focus()
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assert_components(app, selector.query_one("SelectOverlay"), (
                            "option-list--option", "option-list--option-highlighted",
                            "option-list--option-hover", "option-list--option-disabled",
                        ))
                        await pilot.press("escape")
                        await pilot.press("escape")

    async def test_menu_configuration_picker_history_and_approval_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                for theme in THEMES:
                    with self.subTest(theme=theme):
                        app.theme = theme
                        await pilot.pause()
                        app.action_main_menu()
                        await pilot.pause()
                        for widget in app.screen.query("Label, Static, Button"):
                            self.assert_foreground(app, widget)
                        await menus.click(pilot, app, "#menu-connection")
                        editor = app.screen
                        self.assertIsInstance(editor, ConnectionMenu)
                        editor.query_one("#connection-result", Static).update(Text("Error: invalid URL", style="synai.error"))
                        field = editor.query_one("#connection-url", Input)
                        field.value = ""
                        field.placeholder = "Endpoint placeholder"
                        field.focus()
                        await pilot.pause()
                        self.assert_components(app, field, (
                            "input--placeholder", "input--suggestion", "input--cursor", "input--selection",
                        ))
                        for widget in editor.query("Label, Static, Button, Input"):
                            self.assert_foreground(app, widget)
                        await pilot.press("escape")
                        await menus.click(pilot, app, "#menu-environment")
                        settings = app.screen
                        self.assertIsInstance(settings, EnvironmentMenu)
                        settings.query_one("#env-tool_budget", Input).value = "7"
                        settings.update_overview()
                        await pilot.pause()
                        for widget in settings.query("Label, Static, Button, Input, SelectCurrent"):
                            self.assert_foreground(app, widget)
                        active = settings.query_one(".active-page", Button)
                        self.assertTrue(active.has_focus)
                        self.assertEqual(active.styles.background, Color.parse(app.get_css_variables()["surface"]))
                        self.assertEqual(active.styles.border.top[0], "double")
                        settings.query_one("#environment-back", Button).focus()
                        await pilot.pause()
                        self.assertEqual(active.styles.background, Color.parse(app.get_css_variables()["panel"]))
                        self.assertEqual(active.styles.border.top[0], "solid")
                        await pilot.press("escape")
                        app.push_screen(HelpMenu())
                        await pilot.pause()
                        for widget in app.screen.query("Label, Static, Button"):
                            self.assert_foreground(app, widget)
                        await pilot.press("escape")
                        app.open_history_manager()
                        await pilot.pause()
                        history = app.screen
                        self.assertIsInstance(history, HistoryScreen)
                        listing = history.query_one("#history-selection")
                        listing.focus()
                        self.assert_components(app, listing, (
                            "option-list--option-highlighted", "selection-list--button",
                            "selection-list--button-highlighted",
                        ))
                        await pilot.press("escape")
                        picker = DirectoryPicker(root, app.settings)
                        app.push_screen(picker)
                        await pilot.pause()
                        picker.query_one("#directory-error", Static).update(Text("Error: cannot browse", style="synai.error"))
                        await pilot.pause()
                        self.assert_components(app, picker.query_one("#directory-list"), (
                            "option-list--option-highlighted", "option-list--option-disabled",
                        ))
                        for widget in picker.query("Label, Static, Button"):
                            self.assert_foreground(app, widget)
                        await pilot.press("escape")
                        task = asyncio.create_task(app.approve("Warning: model change", "Retained context"))
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ApprovalScreen)
                        for widget in app.screen.query("Label, Static, Button"):
                            self.assert_foreground(app, widget)
                        await pilot.press("escape")
                        self.assertFalse(await task)
                        await pilot.press("escape")
                        app.controls()
