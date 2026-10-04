from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from rich.text import Text
from textual.widgets import Input, OptionList, RichLog, Select, Static, TextArea

import test_menu as menus
from synai.config import Settings
from synai.history import HistoryError
from synai.models import Activity, GenerationSource, Message
from synai.preferences import DEFAULT_THEME, Preferences, PreferencesStore, resolve_connection
from synai.tui.application import ApprovalScreen, CodingApp
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.menu import ConnectionMenu


def colors(log: RichLog) -> set[str]:
    return {str(segment.style.color) for line in log.lines for segment in line
            if segment.style is not None and segment.style.color is not None}


def static_colors(widget: Static) -> set[str]:
    return {str(segment.style.color) for segment in widget.render_line(0)
            if segment.style is not None and segment.style.color is not None}


class GlobalUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for target in ("synai.tui.application.OllamaProvider", "synai.tui.menu.OllamaProvider"):
            mock = patch(target, menus.MenuProvider)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_connection_confirmation_save_cancel_failure_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                original = app.session
                provider = app.provider
                app.query_one("#composer", TextArea).load_text("unsent draft")
                await pilot.press("f2")
                await pilot.pause()
                await menus.click(pilot, app, "#menu-connection")
                self.assertIsInstance(app.screen, ConnectionMenu)
                editor = app.screen
                editor.query_one("#connection-url", Input).value = "http://new:11434/"
                editor.query_one("#connection-timeout", Input).value = "55"
                await menus.click(pilot, app, "#connection-save")
                self.assertIsInstance(app.screen, ApprovalScreen)
                await menus.click(pilot, app, "#deny")
                self.assertIs(app.provider, provider)
                self.assertEqual(app.settings.request_timeout, 1200)
                await menus.click(pilot, app, "#connection-save")
                await menus.click(pilot, app, "#allow")
                self.assertEqual(app.settings.ollama_url, "http://new:11434")
                self.assertEqual(app.settings.request_timeout, 55)
                self.assertIs(app.session, original)
                self.assertEqual(app.query_one("#composer", TextArea).text, "unsent draft")
                provider.close.assert_awaited_once()
                self.assertIs(app.agent.provider, app.provider)
                saved_provider = app.provider
                editor.query_one("#connection-timeout", Input).value = "60"
                with patch.object(app.preferences_store, "save", side_effect=OSError("disk failed")):
                    await menus.click(pilot, app, "#connection-save")
                self.assertIs(app.provider, saved_provider)
                self.assertEqual(app.settings.request_timeout, 55)
                self.assertIn("disk failed", str(editor.query_one("#connection-result", Static).render()))
                editor.query_one("#connection-timeout", Input).value = "nan"
                await menus.click(pilot, app, "#connection-save")
                self.assertIn("finite", str(editor.query_one("#connection-result", Static).render()))
                saved_id = original.session_id
            saved = PreferencesStore(root / "history").load()
            settings, sources = resolve_connection(
                replace(Settings(), history_dir=root / "history"), saved, environ={},
            )
            restarted = CodingApp(settings, preferences=saved, connection_sources=sources)
            restarted.launch_workspace = root
            restarted.approve = AsyncMock(return_value=True)
            async with restarted.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await restarted.load_session(restarted.history.path_for(saved_id))
                self.assertEqual(restarted.settings.ollama_url, "http://new:11434")
                self.assertEqual(restarted.session.endpoint, original.endpoint)
                self.assertEqual(restarted.provider.timeout, 55)

    async def test_confirmed_model_change_preserves_data_and_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                session = app.session
                session.messages = [Message("user", "exact prompt\n"),
                                    Message("assistant", "original answer", source=GenerationSource(
                                        session.model, app.settings.ollama_url,
                                    ))]
                app.history.save(session)
                app.query_one("#composer", TextArea).load_text("draft")
                await pilot.press("f2")
                await pilot.pause()
                dashboard = app.screen
                dashboard.query_one("#model", Select).value = "model:one"
                await pilot.pause()
                self.assertIsInstance(app.screen, ApprovalScreen)
                await menus.click(pilot, app, "#deny")
                self.assertIs(app.session, session)
                self.assertEqual(dashboard.query_one("#model", Select).value, "model:two")
                dashboard.query_one("#model", Select).value = "model:one"
                await pilot.pause()
                await menus.click(pilot, app, "#allow")
                self.assertEqual(app.session.session_id, session.session_id)
                self.assertEqual(app.session.model, "model:one")
                self.assertEqual(app.session.workspace, session.workspace)
                self.assertEqual(app.session.messages, session.messages)
                self.assertEqual(app.query_one("#composer", TextArea).text, "draft")
                app.flush_render()
                self.assertIn("model:two [complete]", "\n".join(line.text for line in app.query_one("#chat", RichLog).lines))
                current = app.session
                dashboard.query_one("#model", Select).value = "model:two"
                await pilot.pause()
                with patch.object(app.history, "save", side_effect=HistoryError("disk failed")):
                    await menus.click(pilot, app, "#allow")
                self.assertIs(app.session, current)
                self.assertEqual(dashboard.query_one("#model", Select).value, "model:one")
                self.assertIn("disk failed", str(app.query_one("#status", Static).render()))

    async def test_themes_change_existing_logs_surfaces_modals_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = menus.MenuTests().app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                app.session.messages = [Message("user", "preserve user"),
                                        Message("assistant", "preserve answer", thinking="preserve thinking")]
                app.session.activity = [Activity("error", "preserve error")]
                raw = app.session.to_dict()
                app.render_dirty = True
                app.flush_render()
                app.query_one("#composer", TextArea).load_text("preserve draft")
                before = {
                    "background": app.screen.styles.background,
                    "border": app.query_one("#chat").styles.border,
                    "chat": colors(app.query_one("#chat", RichLog)),
                    "thinking": colors(app.query_one("#thinking", RichLog)),
                    "activity": colors(app.query_one("#activity", RichLog)),
                }
                status_text = str(app.query_one("#status", Static).render())
                await pilot.press("ctrl+p")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ThemePicker")
                listing = app.screen.query_one("#theme-list", OptionList)
                listing.highlighted = listing.get_option_index("textual-light")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.theme, "textual-light")
                for theme in ("textual-light", "dracula", DEFAULT_THEME):
                    app.theme = theme
                    await pilot.pause()
                    self.assertEqual(app.preferences_store.load().theme, theme)
                    self.assertEqual(app.query_one("#composer", TextArea).text, "preserve draft")
                    self.assertEqual(app.session.to_dict(), raw)
                    self.assertEqual(str(app.query_one("#status", Static).render()), status_text)
                    after = {
                        "background": app.screen.styles.background,
                        "border": app.query_one("#chat").styles.border,
                        "chat": colors(app.query_one("#chat", RichLog)),
                        "thinking": colors(app.query_one("#thinking", RichLog)),
                        "activity": colors(app.query_one("#activity", RichLog)),
                    }
                    if theme != DEFAULT_THEME:
                        for key in before:
                            self.assertNotEqual(before[key], after[key], key)
                    else:
                        self.assertEqual(before, after)
                await pilot.press("f2")
                await pilot.pause()
                await menus.click(pilot, app, "#menu-environment")
                screen = app.screen
                screen.query_one("#env-tool_budget", Input).value = "7"
                screen.update_overview()
                original_text = str(screen.query_one("#configuration-overview", Static).render())
                original_color = screen.query_one(".environment-card").styles.background
                app.theme = "textual-light"
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertNotEqual(screen.query_one(".environment-card").styles.background, original_color)
                self.assertEqual(str(screen.query_one("#configuration-overview", Static).render()), original_text)
                self.assertEqual(screen.query_one("#env-tool_budget", Input).value, "7")
                picker = DirectoryPicker(root, app.settings)
                app.push_screen(picker)
                await pilot.pause()
                picker.query_one("#directory-error", Static).update(Text("retained error", style="synai.error"))
                await pilot.pause()
                error_colors = static_colors(picker.query_one("#directory-error", Static))
                initial = picker.query_one("#directory-picker").styles.background
                app.theme = "dracula"
                await pilot.pause()
                self.assertEqual(picker.current, root)
                self.assertNotEqual(picker.query_one("#directory-picker").styles.background, initial)
                self.assertIn("retained error", str(picker.query_one("#directory-error", Static).render()))
                self.assertNotEqual(static_colors(picker.query_one("#directory-error", Static)), error_colors)
            restarted = menus.MenuTests().app(root)
            self.assertEqual(restarted.theme, "dracula")
            await restarted.provider.close()

    async def test_theme_writes_do_not_clobber_launch_overrides_and_errors_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = Preferences("http://saved:11434", 55)
            store = PreferencesStore(root / "data")
            store.save(saved)
            app = CodingApp(replace(Settings(), history_dir=root / "data", ollama_url="http://override:11434",
                                    request_timeout=1200), preferences=saved)
            app.launch_workspace = root
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                app.theme = "dracula"
                await pilot.pause()
                self.assertEqual(store.load(), replace(saved, theme="dracula"))
                self.assertEqual(app.settings.ollama_url, "http://override:11434")
                with patch.object(app.preferences_store, "save", side_effect=OSError("theme disk failed")):
                    app.theme = "textual-light"
                    await pilot.pause()
                self.assertEqual(app.theme, "textual-light")
                self.assertEqual(store.load().theme, "dracula")
                self.assertIn("not saved", str(app.query_one("#status", Static).render()))
            unknown = CodingApp(replace(Settings(), history_dir=root / "data"),
                                preferences=replace(saved, theme="missing-theme"))
            unknown.launch_workspace = root
            async with unknown.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.assertEqual(unknown.theme, DEFAULT_THEME)
                self.assertIn("unavailable", str(unknown.query_one("#status", Static).render()))

    async def test_offline_global_change_and_missing_model_never_replace_saved_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                original = app.session
                app.sandbox.container_id = "attached"
                app.sandbox.workspace = Path(original.workspace)
                app.host.activate = AsyncMock()
                app.approve = AsyncMock(return_value=True)
                with patch("synai.tui.application.OllamaProvider") as factory:
                    provider = menus.MenuProvider("http://offline:11434")
                    provider.failure = True
                    factory.return_value = provider
                    self.assertTrue(await app.apply_connection("http://offline:11434", 70))
                self.assertIs(app.session, original)
                self.assertEqual(app.session.model, "model:two")
                self.assertEqual(app.sandbox.container_id, "attached")
                self.assertIn("offline", app.connection_error)
                self.assertEqual(app.preferences_store.load().ollama_url, "http://offline:11434")
                self.assertNotIn(app.session.model, app.models)
                provider.failure = False
                provider.list_models = AsyncMock(return_value=[menus.ModelInfo("replacement")])
                await app.refresh_models()
                self.assertEqual(app.session.model, "model:two")
                self.assertNotIn(app.session.model, app.models)
                app.sandbox.container_id = None
                self.assertTrue(await app.change_model("replacement"))
                self.assertEqual(app.session.session_id, original.session_id)
                self.assertEqual(app.session.model, "replacement")

    async def test_theme_change_during_stream_preserves_output_and_request_provenance(self) -> None:
        class StreamingProvider(menus.MenuProvider):
            async def chat(self, model, messages, tools):
                yield menus.ChatEvent(content="first\n", thinking="thinking\n")
                started.set()
                await finish.wait()
                yield menus.ChatEvent(content="last\n", done=True)

        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            started, finish = asyncio.Event(), asyncio.Event()
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                await app.provider.close()
                provider = StreamingProvider(app.settings.ollama_url)
                app.provider = app.agent.provider = provider
                app.query_one("#composer", TextArea).load_text("exact input\n")
                await app.action_send()
                await started.wait()
                app.query_one("#composer", TextArea).load_text("next draft")
                app.theme = "textual-light"
                await pilot.pause()
                self.assertEqual(app.session.messages[-1].content, "first\n")
                self.assertEqual(app.query_one("#composer", TextArea).text, "next draft")
                finish.set()
                await app.turn_task
                self.assertEqual(app.session.messages[-1].content, "first\nlast\n")
                self.assertEqual(app.session.messages[-1].thinking, "thinking\n")
                self.assertEqual(app.session.messages[-1].source,
                                 GenerationSource("model:two", app.settings.ollama_url))
                self.assertEqual(app.history.load(app.history.path_for(app.session.session_id)).messages[-1].content,
                                 "first\nlast\n")

    async def test_old_history_connection_is_preserved_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = menus.MenuTests().app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await menus.MenuTests().create(pilot, app)
                session = app.session
                path = app.history.path_for(session.session_id)
                data = session.to_dict()
                data["schema_version"] = 4
                data.pop("legacy_request_timeout")
                data["endpoint"] = "http://old:11434"
                data["environment"]["ollama_url"] = data["endpoint"]
                data["environment"]["request_timeout"] = 65
                data["messages"] = [asdict(Message("assistant", "unchanged raw\n", thinking="old thinking"))]
                path.write_text(json.dumps(data))
                before = path.read_bytes()
                loaded = app.history.load(path)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(loaded.messages[0].source, GenerationSource("model:two", "http://old:11434", legacy=True))
                app.approve = AsyncMock(return_value=True)
                await app.load_session(path)
                self.assertEqual(app.settings.ollama_url, app.launch_settings.ollama_url)
                self.assertEqual(app.session.endpoint, "http://old:11434")
                self.assertEqual(app.session.legacy_request_timeout, 65)
                upgraded = json.loads(path.read_text())
                self.assertEqual(upgraded["schema_version"], 5)
                self.assertNotIn("ollama_url", upgraded["environment"])
                self.assertNotIn("source", app.session.messages[0].wire())
                self.assertEqual(app.session.messages[0].content, "unchanged raw\n")
