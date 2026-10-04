from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widgets import Button, Input, OptionList, Select, TextArea

import test_menu as menus
from synai.models import ChatEvent, Session
from support import save_managed
from test_keyboard_dashboard import activate_with_tab, focus_with_tab
from synai.tui.application import ApprovalScreen, ConversationList, HistoryScreen
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import ConnectionMenu, EnvironmentMenu, MainMenu, SandboxSwitch
from synai.tui.navigation import directional_score


class DirectionalScoreTests(unittest.TestCase):
    def test_directions_edges_alignment_and_unequal_sizes(self) -> None:
        origin = Region(10, 10, 20, 3)
        for direction, target in (
            ("up", Region(10, 3, 30, 3)),
            ("down", Region(10, 15, 10, 5)),
            ("left", Region(0, 10, 5, 3)),
            ("right", Region(35, 10, 5, 3)),
        ):
            with self.subTest(direction=direction):
                self.assertIsNotNone(directional_score(origin, target, direction))
                self.assertIsNone(directional_score(origin, origin, direction))
        aligned = directional_score(origin, Region(10, 30, 20, 3), "down")
        diagonal = directional_score(origin, Region(40, 14, 20, 3), "down")
        self.assertLess(aligned, diagonal)
        close = directional_score(origin, Region(10, 14, 20, 3), "down")
        self.assertLess(close, aligned)
        self.assertIsNone(directional_score(origin, Region(10, 3, 20, 3), "down"))


class MenuNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_dashboard_columns_scrolling_edges_resize_and_hidden_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.assertEqual(app.screen.focused.id, "menu-new")
                await pilot.press("up", "left")
                self.assertEqual(app.screen.focused.id, "menu-new")
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, "menu-histories")
                await pilot.press("right")
                self.assertEqual(app.screen.focused.id, "menu-connection")
                await pilot.press("down", "down", "down", "down")
                await pilot.pause()
                self.assertEqual(app.screen.focused.id, "menu-quit")
                self.assertTrue(app.screen.focused.region.overlaps(
                    app.screen.query_one("#dashboard-content").region,
                ))
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, "menu-quit")
                await pilot.resize_terminal(60, 24)
                await pilot.pause()
                self.assertTrue(app.screen.has_class("narrow"))
                await pilot.press("right")
                self.assertEqual(app.screen.focused.id, "menu-quit")
                await focus_with_tab(pilot, app, "menu-histories")
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, "menu-connection")
                await pilot.resize_terminal(140, 45)
                await pilot.pause()
                await pilot.press("left")
                self.assertIn(app.screen.focused.id, {"menu-new", "menu-histories"})
                dashboard = app.screen
                await focus_with_tab(pilot, app, "menu-connection")
                app.loading = True
                dashboard.update_status()
                await pilot.pause()
                await pilot.press("down")
                self.assertEqual(dashboard.focused.id, "menu-theme")
                app.loading = False

    async def test_configuration_arrows_enter_input_without_mutating_page_or_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, EnvironmentMenu)
                await focus_with_tab(pilot, app, "config-nav-overview")
                await pilot.press("down", "down", "down")
                self.assertEqual(screen.focused.id, "config-nav-limits")
                self.assertEqual(screen.query_one("#configuration-pages").current, "config-page-limits")
                await pilot.press("enter")
                await pilot.pause()
                baseline = screen.form_values()
                await pilot.press("right")
                await pilot.pause()
                self.assertIsInstance(screen.focused, Input)
                field = screen.focused
                await pilot.press("enter", "end", "left")
                self.assertEqual(field.cursor_position, len(field.value) - 1)
                await pilot.press("up", "down")
                self.assertIs(screen.focused, field)
                self.assertEqual(screen.form_values(), baseline)
                await focus_with_tab(pilot, app, "environment-apply")
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "environment-back")
                await pilot.press("left")
                self.assertEqual(screen.focused.id, "environment-apply")

    async def test_model_dropdown_and_connection_field_arrows_remain_native(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                await focus_with_tab(pilot, app, "menu-environment")
                await pilot.press("up")
                selector = app.screen.query_one("#model", Select)
                self.assertIs(app.screen.focused, selector)
                original = app.session.model
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(selector.expanded)
                await pilot.press("home", "down", "escape")
                await pilot.pause()
                self.assertEqual(app.session.model, original)
                await pilot.press("tab")
                self.assertIsNot(app.screen.focused, selector)
                await pilot.press("alt+o")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConnectionMenu)
                field = app.screen.query_one(Input)
                await focus_with_tab(pilot, app, field.id)
                original_text = field.value
                await pilot.press("enter", "end", "left", "left")
                self.assertEqual(field.cursor_position, len(original_text) - 2)
                await pilot.press("up", "down")
                self.assertIs(app.screen.focused, field)
                self.assertEqual(field.value, original_text)
                await pilot.press("tab")
                self.assertIsNot(app.screen.focused, field)

    async def test_conversation_list_retains_selection_and_footer_arrows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            for title in ("first", "second"):
                save_managed(app, Session("model:one", app.settings.ollama_url, directory, title=title))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("alt+c")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, HistoryScreen)
                listing = screen.query_one(ConversationList)
                await pilot.press("home", "space", "down")
                self.assertEqual(listing.highlighted, 1)
                self.assertEqual(len(listing.selected), 1)
                self.assertIs(screen.focused, listing)
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(screen.focused.id, "open-highlighted")
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "delete-selected")
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "close-history")
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "close-history")
                await pilot.press("up")
                self.assertIs(screen.focused, listing)
                self.assertIsNone(app.session)
                self.assertEqual(len(app.history.list_paths()), 2)

    async def test_directory_list_arrows_and_button_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "b").mkdir()
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                app.push_screen(DirectoryPicker(root, app.settings))
                await pilot.pause()
                screen = app.screen
                listing = screen.query_one(OptionList)
                await pilot.press("home", "down")
                self.assertEqual(listing.highlighted, 1)
                self.assertIs(screen.focused, listing)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen.current, root / "b")
                await focus_with_tab(pilot, app, "directory-up")
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "directory-home")
                await pilot.press("down")
                self.assertIs(screen.focused, listing)
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(screen.focused.id, "directory-choose")
                await pilot.press("right")
                self.assertEqual(screen.focused.id, "directory-cancel")
                await pilot.press("left")
                self.assertEqual(screen.focused.id, "directory-choose")

    async def test_approval_and_handoff_arrows_never_activate_or_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                task = asyncio.create_task(app.approve("Delete?", "Must explicitly confirm", destructive=True))
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, ApprovalScreen)
                self.assertEqual(screen.focused.id, "deny")
                await pilot.press("left")
                self.assertEqual(screen.focused.id, "allow")
                await pilot.press("up", "down", "f2", "f3")
                self.assertIs(app.screen, screen)
                self.assertFalse(task.done())
                await pilot.press("right", "enter")
                self.assertFalse(await task)
                app.push_screen(SandboxSwitch("fixture", owned=True))
                await pilot.pause()
                await focus_with_tab(pilot, app, "switch-cancel")
                await pilot.press("down", "down")
                self.assertEqual(app.screen.focused.id, "switch-remove")
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, "switch-remove")
                self.assertIsInstance(app.screen, SandboxSwitch)
                await pilot.press("escape")
                self.assertIsInstance(app.screen, MainMenu)

    async def test_streaming_skips_disabled_actions_and_preserves_stop(self) -> None:
        started = asyncio.Event()

        class SlowProvider(menus.MenuProvider):
            async def chat(self, model, messages, tools):
                yield ChatEvent(content="partial")
                started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                app.provider = app.agent.provider = SlowProvider()
                app.query_one("#composer", TextArea).load_text("go")
                await pilot.press("ctrl+s")
                await started.wait()
                await pilot.press("f2")
                await pilot.pause()
                await focus_with_tab(pilot, app, "menu-back")
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, "menu-details-open")
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, "menu-stop")
                self.assertIsNotNone(app.turn_task)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsNone(app.turn_task)
                self.assertEqual(app.session.state, "cancelled")
                self.assertEqual(len(app.history.list_paths()), 1)

    async def test_scroll_container_no_focus_and_edge_arrows_never_scroll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                screen = app.screen
                scroll = screen.query_one("#dashboard-content", VerticalScroll)
                scroll.focus()
                await pilot.pause()
                self.assertFalse(scroll.can_focus)
                self.assertIsNot(screen.focused, scroll)
                await focus_with_tab(pilot, app, "menu-new")
                original = scroll.scroll_offset
                await pilot.press("up", "left")
                await pilot.pause()
                self.assertEqual(screen.focused.id, "menu-new")
                self.assertEqual(scroll.scroll_offset, original)
                await pilot.press("pagedown")
                await pilot.pause()
                self.assertGreater(scroll.scroll_y, original.y)
                original = scroll.scroll_offset
                screen.set_focus(None, scroll_visible=False)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(scroll.scroll_offset, original)
                self.assertIsNotNone(screen.focused)
                self.assertFalse(scroll.vertical_scrollbar.can_focus)
                await focus_with_tab(pilot, app, "menu-quit")
                await pilot.pause()
                original = scroll.scroll_offset
                await pilot.press("down", "right")
                await pilot.pause()
                self.assertEqual(screen.focused.id, "menu-quit")
                self.assertEqual(scroll.scroll_offset, original)
                await focus_with_tab(pilot, app, "menu-new")
                scroll.scroll_to(y=0, animate=False, force=True)
                await pilot.pause()
                top = scroll.scroll_offset
                await pilot.press("right", "down", "down", "down", "down")
                await pilot.pause()
                self.assertEqual(screen.focused.id, "menu-quit")
                self.assertGreater(scroll.scroll_y, top.y)
                self.assertTrue(screen.focused.region.overlaps(scroll.region))

    async def test_unused_input_and_list_arrows_do_not_scroll_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("alt+n")
                await pilot.pause()
                screen = app.screen
                screen.show_page("limits")
                await pilot.pause()
                await focus_with_tab(pilot, app, "env-tool_budget")
                await pilot.pause()
                field = screen.focused
                await pilot.press("enter")
                scroll = screen.query_one("#config-page-limits", VerticalScroll)
                original = scroll.scroll_offset
                await pilot.press("down", "down", "up", "up")
                await pilot.pause()
                self.assertIs(screen.focused, field)
                self.assertEqual(scroll.scroll_offset, original)
                await pilot.press("end", "left")
                self.assertEqual(field.cursor_position, len(field.value) - 1)
                screen.query_one("#config-nav-sandbox", Button).focus()
                await pilot.pause()
                screen.query_one("#env-execution_mode", Select).focus()
                scroll = screen.query_one("#config-page-sandbox", VerticalScroll)
                await pilot.pause()
                original = scroll.scroll_offset
                await pilot.press("left", "right")
                await pilot.pause()
                self.assertEqual(scroll.scroll_offset, original)
                selector = screen.query_one("#env-execution_mode", Select)
                self.assertFalse(selector.expanded)
                selector.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(selector.expanded)
                original = scroll.scroll_offset
                overlay = screen.focused
                await pilot.press("left", "right")
                await pilot.pause()
                self.assertIs(screen.focused, overlay)
                self.assertEqual(scroll.scroll_offset, original)
                await pilot.press("escape", "escape")
                await pilot.pause()
                root = Path(directory)
                for name in ("a", "b"):
                    (root / name).mkdir()
                app.push_screen(DirectoryPicker(root, app.settings))
                await pilot.pause()
                picker = app.screen
                listing = picker.query_one(OptionList)
                original = listing.scroll_offset
                highlighted = listing.highlighted
                await pilot.press("left", "right")
                await pilot.pause()
                self.assertEqual(listing.scroll_offset, original)
                self.assertEqual(listing.highlighted, highlighted)
                await pilot.press("down")
                self.assertNotEqual(listing.highlighted, highlighted)


if __name__ == "__main__":
    unittest.main()
