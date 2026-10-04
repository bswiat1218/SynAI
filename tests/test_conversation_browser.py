from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.color import Color
from textual.widgets import Button, Static, TextArea

import test_menu as menus
from synai.history import HistoryError
from synai.models import Message, Session
from support import save_managed
from synai.tui.application import ConversationList, HistoryScreen
from synai.tui.menu import MainMenu


class ConversationBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    def saved(self, app, title: str) -> Session:
        session = Session("model:one", app.settings.ollama_url, str(app.launch_workspace), title=title)
        save_managed(app, session)
        return session

    async def test_deletion_marker_is_red_with_and_without_highlight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            first, second = self.saved(app, "Selected"), self.saved(app, "Not selected")
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                listing = app.screen.query_one(ConversationList)
                listing.select(first.session_id)
                for theme in ("synai-cyberpunk", "textual-light", "dracula"):
                    app.theme = theme
                    await pilot.pause()
                    foreground = Color.parse(app.get_css_variables()["foreground"])
                    for highlighted in (first.session_id, second.session_id):
                        menus.highlight_history(app, highlighted)
                        await pilot.pause()
                        row = list(app.screen.paths).index(first.session_id)
                        segments = list(listing.render_line(row))
                        self.assertEqual(Color.from_rich_color(segments[1].style.color), Color.parse("#ff5555"))
                        self.assertEqual(Color.from_rich_color(segments[4].style.color), foreground)
                    listing.deselect(first.session_id)
                    await pilot.pause()
                    segments = list(listing.render_line(row))
                    self.assertEqual(Color.from_rich_color(segments[1].style.color), foreground)
                    listing.select(first.session_id)

    async def test_enter_opens_highlight_not_checked_rows_and_space_only_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            first, second = self.saved(app, "First"), self.saved(app, "Second")
            third = self.saved(app, "Third")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                screen = app.screen
                listing = screen.query_one(ConversationList)
                self.assertFalse(screen.query_one("#open-highlighted", Button).disabled)
                self.assertTrue(screen.query_one("#delete-selected", Button).disabled)
                menus.highlight_history(app, first.session_id)
                listing.focus()
                await pilot.press("space")
                await pilot.pause()
                self.assertEqual(listing.selected, [first.session_id])
                self.assertIsNone(app.session)
                listing.select(third.session_id)
                menus.highlight_history(app, second.session_id)
                await pilot.pause()
                checked = list(listing.selected)
                for identifier in ("open-highlighted", "delete-selected", "close-history"):
                    button = screen.query_one(f"#{identifier}", Button)
                    self.assertGreater(button.region.width, 15)
                    self.assertLessEqual(button.region.right, 80)
                    self.assertLessEqual(button.region.bottom, 24)
                with patch.object(app, "load_session", wraps=app.load_session) as opening:
                    await pilot.press("enter")
                    await pilot.pause()
                    opening.assert_awaited_once_with(app.history.path_for(second.session_id))
                self.assertEqual(listing.selected, checked)
                self.assertEqual(app.session.session_id, second.session_id)
                self.assertFalse(app.history_manager_open)
                self.assertNotIsInstance(app.screen, (MainMenu, HistoryScreen))
                self.assertIs(app.screen.focused, app.query_one("#composer", TextArea))
                self.assertTrue(app.history.path_for(first.session_id).exists())
                self.assertTrue(app.history.path_for(second.session_id).exists())

    async def test_sandbox_handoff_cancel_preserves_active_conversation_and_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                current = app.session
                other = self.saved(app, "Other")
                app.sandbox.container_id = "keep-running"
                app.sandbox.workspace = Path(current.workspace)
                app.query_one("#composer", TextArea).load_text("retained draft")
                app.action_main_menu()
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                screen = app.screen
                menus.highlight_history(app, other.session_id)
                await menus.click(pilot, app, "#open-highlighted")
                await menus.click(pilot, app, "#switch-cancel")
                self.assertIs(app.screen, screen)
                self.assertIs(app.session, current)
                self.assertEqual(app.sandbox.container_id, "keep-running")
                self.assertEqual(app.query_one("#composer", TextArea).text, "retained draft")
                self.assertIn("cancelled", str(screen.query_one("#history-result", Static).render()))
                self.assertFalse(screen.query_one("#open-highlighted", Button).disabled)

    async def test_close_keeps_draft_and_theme_switch_keeps_highlight_and_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                current = app.session
                other = self.saved(app, "Other")
                app.query_one("#composer", TextArea).load_text("retained draft")
                app.action_main_menu()
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                screen = app.screen
                listing = screen.query_one(ConversationList)
                menus.highlight_history(app, other.session_id)
                listing.select(current.session_id)
                highlighted = listing.highlighted
                app.theme = "textual-light"
                await pilot.pause()
                self.assertEqual(listing.highlighted, highlighted)
                self.assertEqual(listing.selected, [current.session_id])
                expected = Color.parse(app.get_css_variables()["foreground"])
                for button in screen.query(Button):
                    self.assertEqual(button.styles.color, expected)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, MainMenu)
                self.assertFalse(app.history_manager_open)
                self.assertIs(app.session, current)
                self.assertEqual(app.query_one("#composer", TextArea).text, "retained draft")

    async def test_open_failure_and_cancel_keep_browser_and_prevent_overlapping_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            saved = self.saved(app, "Different server")
            saved.endpoint = "http://other:11434"
            saved.messages = [Message("user", "Retained context")]
            app.history.save(saved)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                screen = app.screen
                listing = screen.query_one(ConversationList)
                listing.select(saved.session_id)
                with patch.object(app, "load_session", wraps=app.load_session) as opening:
                    listing.focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(screen.opening)
                    self.assertTrue(listing.disabled)
                    screen.start_open()
                    screen.action_close()
                    await menus.click(pilot, app, "#deny")
                    self.assertIs(app.screen, screen)
                    opening.assert_awaited_once()
                self.assertFalse(screen.opening)
                self.assertEqual(listing.selected, [saved.session_id])
                self.assertIsNone(app.session)
                self.assertIn("cancelled", str(screen.query_one("#history-result", Static).render()))
                with patch.object(app, "load_session", side_effect=HistoryError("record disappeared")):
                    await menus.click(pilot, app, "#open-highlighted")
                self.assertIn("record disappeared", str(screen.query_one("#history-result", Static).render()))
                self.assertFalse(screen.query_one("#close-history", Button).disabled)

    async def test_partial_delete_preserves_failed_checks_and_highlight_then_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            first, second = self.saved(app, "Delete"), self.saved(app, "Keep on failure")
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                screen = app.screen
                listing = screen.query_one(ConversationList)
                menus.highlight_history(app, second.session_id)
                listing.select(first.session_id)
                listing.select(second.session_id)
                delete = app.history.delete

                def fail_one(path):
                    if app.history.identifier(path) == second.session_id:
                        raise HistoryError("delete failed")
                    delete(path)

                with patch.object(app.history, "delete", side_effect=fail_one):
                    await menus.click(pilot, app, "#delete-selected")
                    screen.start_open()
                    self.assertFalse(screen.opening)
                    await menus.click(pilot, app, "#allow")
                self.assertEqual(listing.selected, [second.session_id])
                self.assertEqual(listing.get_option_at_index(listing.highlighted).value, second.session_id)
                self.assertIn("delete failed", str(screen.query_one("#history-result", Static).render()))
                await menus.click(pilot, app, "#delete-selected")
                await menus.click(pilot, app, "#deny")
                self.assertEqual(listing.selected, [second.session_id])
                await menus.click(pilot, app, "#delete-selected")
                await menus.click(pilot, app, "#allow")
                self.assertEqual(screen.paths, {})
                self.assertIsNone(listing.highlighted)
                self.assertTrue(screen.query_one("#open-highlighted", Button).disabled)
                self.assertTrue(screen.query_one("#delete-selected", Button).disabled)
                self.assertIsNone(app.session)

    async def test_listing_error_clears_stale_rows_and_disables_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            saved = self.saved(app, "Saved")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await menus.click(pilot, app, "#menu-histories")
                screen = app.screen
                listing = screen.query_one(ConversationList)
                listing.select(saved.session_id)
                with patch.object(app, "history_entries", side_effect=OSError("list failed")):
                    screen.refresh_entries()
                await pilot.pause()
                self.assertEqual(screen.paths, {})
                self.assertEqual(listing.option_count, 0)
                self.assertTrue(screen.query_one("#open-highlighted", Button).disabled)
                self.assertTrue(screen.query_one("#delete-selected", Button).disabled)
                self.assertIn("list failed", str(screen.query_one("#history-result", Static).render()))
