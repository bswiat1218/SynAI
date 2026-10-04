from __future__ import annotations

from support import save_managed

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, ContentSwitcher, Input, Select, SelectionList, Static, TextArea

from synai.config import ConversationEnvironment, Settings
from synai.history import History, HistoryError
from synai.models import ChatEvent, Message, ModelInfo, Session
from synai.providers.base import ProviderError
from synai.sandbox import SandboxError
from synai.tui.application import CodingApp, HistoryScreen
from synai.tui.menu import ConversationMenu, EnvironmentMenu, HelpMenu, MainMenu, SandboxSwitch


class MenuProvider:
    def __init__(self, url: str = "", timeout: float = 1200) -> None:
        self.url, self.timeout = url, timeout
        self.failure = False
        self.close = AsyncMock()

    async def list_models(self) -> list[ModelInfo]:
        if self.failure:
            raise ProviderError("Ollama is offline")
        return [ModelInfo("model:one"), ModelInfo("model:two")]

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name, tools=True)

    async def chat(self, model, messages, tools):
        yield ChatEvent(content="Hello", done=True)


async def click(pilot, app: CodingApp, identifier: str) -> None:
    if isinstance(app.screen, EnvironmentMenu) and identifier in {"#config-create", "#config-attach", "#config-host-enable"}:
        app.screen.show_page("sandbox")
    app.screen.query_one(identifier, Button).scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click(identifier)
    await pilot.pause()


def highlight_history(app: CodingApp, identifier: str) -> None:
    screen = app.screen
    choices = screen.query_one("#history-selection", SelectionList)
    choices.highlighted = list(screen.paths).index(identifier)


class MenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.patch_app = patch("synai.tui.application.OllamaProvider", MenuProvider)
        self.patch_menu = patch("synai.tui.menu.OllamaProvider", MenuProvider)
        self.patch_app.start()
        self.patch_menu.start()
        self.addCleanup(self.patch_app.stop)
        self.addCleanup(self.patch_menu.stop)

    def app(self, root: Path) -> CodingApp:
        app = CodingApp(replace(Settings(), history_dir=root / "history"))
        app.launch_workspace = root
        return app

    async def create(self, pilot, app: CodingApp, *, model: str = "model:two") -> None:
        await click(pilot, app, "#menu-new")
        self.assertIsInstance(app.screen, EnvironmentMenu)
        await click(pilot, app, "#environment-apply")
        self.assertIsInstance(app.screen, ConversationMenu)
        app.screen.query_one("#conversation-choice", Select).value = model
        await click(pilot, app, "#conversation-open")

    async def test_grouped_main_menu_hides_duplicate_startup_routes_and_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertFalse(screen.query_one("#menu-back", Button).display)
                self.assertFalse(screen.query_one("#menu-current-group").display)
                self.assertFalse(screen.query_one("#menu-legacy", Button).display)
                visible = [button.id for button in screen.focus_chain if isinstance(button, Button)]
                self.assertEqual(visible, [
                    "menu-new", "menu-histories", "menu-connection", "menu-refresh",
                    "menu-theme", "menu-quit",
                ])
                screen.query_one("#menu-new", Button).focus()
                await pilot.press("shift+tab")
                self.assertNotIn(screen.focused.id, {"menu-environment", "menu-back", "menu-legacy"})
                app.legacy_retry = True
                screen.update_status()
                self.assertTrue(screen.query_one("#menu-legacy", Button).display)
                app.storage_error = "Storage unavailable"
                screen.update_status()
                self.assertTrue(screen.query_one("#menu-new", Button).disabled)
                self.assertTrue(screen.query_one("#menu-histories", Button).disabled)
                self.assertIn("Storage unavailable", str(screen.query_one("#menu-status", Static).render()))

    async def test_active_dashboard_shares_host_status_and_keeps_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                for identifier in ("history", "edit-environment", "main-menu", "disconnect"):
                    self.assertEqual(len(app.query(f"#{identifier}")), 0)
                self.assertEqual(len(app.query("#manage-history")), 0)
                with patch.object(app, "approve", AsyncMock(return_value=True)):
                    await app.apply_environment(replace(app.settings, execution_mode="host"), workspace)
                self.assertEqual(len(app.query("#image")), 0)
                await pilot.press("f2")
                await pilot.pause()
                screen = app.screen
                self.assertTrue(screen.query_one("#menu-current-group").display)
                visible = [button.id for button in screen.query(Button) if button.display]
                self.assertEqual(visible[:3], ["menu-new", "menu-histories", "menu-back"])
                status = str(screen.query_one("#capability", Static).render())
                self.assertIn("HOST // NOT ISOLATED", status)
                self.assertIn("Tools approved", status)
                self.assertNotIn("no sandbox attached", status)
                app.host.revoke()
                app.update_execution_status()
                screen.update_status()
                self.assertIn("not authorized", str(screen.query_one("#capability", Static).render()))
                await click(pilot, app, "#menu-environment")
                settings = app.screen
                self.assertEqual([title for title, _ in settings.PAGES.values()], [
                    "Overview", "Execution", "Workspace", "Limits",
                ])
                settings.show_page("sandbox")
                await pilot.pause()
                self.assertFalse(settings.query_one("#env-image", Input).display)
                self.assertFalse(settings.query_one("#env-runtime", Select).display)
                self.assertEqual(str(settings.query_one("#environment-apply", Button).label), "SAVE SETTINGS")
                settings.query_one("#env-execution_mode", Select).value = "sandbox"
                await pilot.pause()
                self.assertTrue(settings.query_one("#env-image", Input).display)
                self.assertLess(
                    settings.query_one("#env-execution_mode", Select).region.y,
                    settings.query_one("#env-image", Input).region.y,
                )
                await pilot.press("escape")
                await pilot.pause()
                await click(pilot, app, "#menu-back")
                self.assertEqual(app.session.environment.execution_mode, "host")

    async def test_host_new_chat_denial_enable_disconnect_and_inactive_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            app = self.app(root)
            app.launch_workspace = workspace
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                screen = app.screen
                await click(pilot, app, "#config-nav-sandbox")
                screen.query_one("#env-execution_mode", Select).value = "host"
                await pilot.pause()
                self.assertTrue(screen.query_one("#config-host-warning", Static).display)
                self.assertTrue(screen.query_one("#env-runtime", Select).disabled)
                self.assertTrue(screen.query_one("#env-image", Input).disabled)
                self.assertFalse(app.host.matches(Session("m", app.settings.ollama_url, str(workspace))))
                await click(pilot, app, "#config-nav-limits")
                self.assertTrue(screen.query_one("#env-memory", Input).disabled)
                self.assertFalse(screen.query_one("#env-command_timeout", Input).disabled)
                self.assertIn("NOT enforced", str(screen.query_one("#config-limit-help", Static).render()))
                screen.query_one("#env-tool_budget", Input).value = "8"
                await click(pilot, app, "#environment-apply")
                await click(pilot, app, "#conversation-open")
                self.assertIsNotNone(app.approval_future)
                self.assertIn("NO SANDBOX", str(app.screen.query_one("#approval-title", Static).render()))
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.session.environment.execution_mode, "host")
                self.assertEqual(app.session.environment.tool_budget, 8)
                self.assertFalse(app.host.matches(app.session))
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-environment")
                await click(pilot, app, "#config-host-enable")
                await click(pilot, app, "#allow")
                self.assertTrue(app.host.matches(app.session))
                self.assertTrue(app.screen.query_one("#config-host-enable", Button).disabled)
                self.assertFalse(app.screen.query_one("#config-create", Button).display)
                await pilot.press("escape")
                await pilot.pause()
                await click(pilot, app, "#menu-back")
                self.assertIn("NOT ISOLATED", str(app.query_one("#conversation-summary", Static).render()))
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#disconnect")
                await pilot.pause()
                self.assertFalse(app.host.matches(app.session))
                self.assertEqual(app.session.environment.execution_mode, "host")
                self.assertIsNone(app.approval_future)
                self.assertTrue(workspace.exists())

    async def test_resumed_host_requires_fresh_consent_and_does_not_persist_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            app = self.app(root)
            saved = Session("model:one", app.settings.ollama_url, str(workspace))
            saved.set_environment(ConversationEnvironment.from_settings(
                replace(app.settings, execution_mode="host"), workspace,
            ))
            save_managed(app, saved)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-histories")
                highlight_history(app, saved.session_id)
                await click(pilot, app, "#open-highlighted")
                self.assertFalse(app.host.matches(app.session))
                await click(pilot, app, "#allow")
                self.assertTrue(app.host.matches(app.session))
                session_id = app.session.session_id
            self.assertIsNone(app.host.session_id)
            restarted = self.app(root)
            async with restarted.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await click(pilot, restarted, "#menu-histories")
                highlight_history(restarted, session_id)
                await click(pilot, restarted, "#open-highlighted")
                self.assertIsNotNone(restarted.approval_future)
                self.assertFalse(restarted.host.matches(restarted.session))
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(restarted.host.matches(restarted.session))
                self.assertEqual(restarted.session.environment.execution_mode, "host")
                self.assertIsNone(restarted.session.container_id)

    async def test_mode_switch_save_failure_container_guard_and_host_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            app = self.app(root)
            app.launch_workspace = workspace
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                original = app.session
                app.sandbox.container_id = "attached"
                with self.assertRaisesRegex(ValueError, "Disconnect"):
                    await app.apply_environment(replace(app.settings, execution_mode="host"), workspace)
                self.assertIs(app.session, original)
                app.sandbox.detach()
                with patch.object(app.history, "save", side_effect=HistoryError("disk unavailable")):
                    with self.assertRaises(HistoryError):
                        await app.apply_environment(replace(app.settings, execution_mode="host"), workspace)
                self.assertEqual(app.settings.execution_mode, "sandbox")
                self.assertFalse(app.host.matches(app.session))
                approval = AsyncMock(return_value=True)
                with patch.object(app, "approve", approval):
                    await app.apply_environment(replace(app.settings, execution_mode="host"), workspace)
                    self.assertTrue(app.host.matches(app.session))
                    self.assertEqual(approval.await_count, 1)
                    with patch.object(app.history, "save", side_effect=HistoryError("disk unavailable")):
                        with self.assertRaises(HistoryError):
                            await app.apply_environment(replace(app.settings, execution_mode="sandbox"),
                                                        app.storage.workspace(app.session.session_id))
                    self.assertTrue(app.host.matches(app.session))
                    self.assertEqual(app.settings.execution_mode, "host")
                    await app.apply_environment(replace(app.settings, tool_budget=5), workspace)
                    self.assertTrue(app.host.matches(app.session))
                    self.assertEqual(approval.await_count, 1)
                    other = root / "other"
                    other.mkdir()
                    await app.apply_environment(app.settings, other)
                    self.assertEqual(app.host.workspace, other)
                    self.assertEqual(approval.await_count, 2)
                    await app.apply_environment(replace(app.settings, execution_mode="sandbox"),
                                                app.storage.workspace(app.session.session_id))
                    self.assertFalse(app.host.matches(app.session))
                    self.assertIsNone(app.host.session_id)
                    self.assertIs(app.agent.tools.sandbox, app.sandbox)

    async def test_host_activation_error_remains_visible_and_never_uses_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            app = self.app(root)
            app.launch_workspace = workspace
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                app.sandbox.execute = AsyncMock()
                with patch.object(app, "approve", AsyncMock(return_value=True)), patch(
                    "synai.host.os.geteuid", return_value=0,
                ):
                    self.assertTrue(await app.apply_environment(
                        replace(app.settings, execution_mode="host"), workspace,
                    ))
                self.assertFalse(app.host.matches(app.session))
                self.assertIn("cannot run as root", app.execution_status())
                self.assertIn("cannot run as root", app.host_error)
                self.assertIs(app.agent.tools.sandbox, app.host)
                app.sandbox.execute.assert_not_awaited()

    async def test_draft_host_mode_never_activates_or_bypasses_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-environment")
                await click(pilot, app, "#config-nav-sandbox")
                app.screen.query_one("#env-execution_mode", Select).value = "host"
                await pilot.pause()
                await click(pilot, app, "#config-host-enable")
                self.assertIn("Save environment changes", str(app.screen.query_one("#environment-result", Static).render()))
                self.assertIsNone(app.approval_future)
                self.assertEqual(app.settings.execution_mode, "sandbox")
                self.assertIsNone(app.host.session_id)
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.session.environment.execution_mode, "sandbox")

    async def test_pages_preserve_draft_and_save_all_fields_without_navigation_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-environment")
                screen = app.screen
                app.provider.list_models = AsyncMock(return_value=list(app.models.values()))
                app.sandbox.pull = AsyncMock()
                app.sandbox.create = AsyncMock()
                app.sandbox.attach = AsyncMock()
                for page, key, value in (
                    ("sandbox", "image", "custom:latest"),
                    ("limits", "tool_budget", "7"),
                ):
                    await click(pilot, app, f"#config-nav-{page}")
                    self.assertEqual(screen.query_one(ContentSwitcher).current, f"config-page-{page}")
                    screen.query_one(f"#env-{key}", Input).value = value
                screen.query_one("#env-runtime", Select).value = "podman"
                await click(pilot, app, "#config-nav-overview")
                overview = str(screen.query_one("#configuration-overview", Static).render())
                self.assertIn("Unsaved changes", overview)
                self.assertIn(app.session.workspace, overview)
                self.assertIn("podman / custom:latest", overview)
                self.assertIn("Tool budget: 7", overview)
                self.assertEqual(app.session.environment.tool_budget, 20)
                app.provider.list_models.assert_not_awaited()
                app.sandbox.pull.assert_not_awaited()
                app.sandbox.create.assert_not_awaited()
                app.sandbox.attach.assert_not_awaited()
                await click(pilot, app, "#environment-apply")
                saved = app.history.load(app.history.path_for(app.session.session_id))
                self.assertEqual(saved.environment.workspace, app.session.workspace)
                self.assertEqual(app.settings.request_timeout, app.launch_settings.request_timeout)
                self.assertEqual(saved.environment.tool_budget, 7)
                self.assertEqual(saved.environment.image, "custom:latest")
                self.assertEqual(saved.environment.runtime, "podman")
                self.assertNotIn("Unsaved changes", str(screen.query_one("#configuration-draft-status", Static).render()))
                await click(pilot, app, "#config-nav-limits")
                self.assertEqual(screen.query_one("#env-tool_budget", Input).value, "7")

    async def test_hidden_invalid_fields_open_page_and_focus_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                screen = app.screen
                cases = (
                    ("image", "-invalid", "sandbox"),
                    ("command_timeout", "bad", "limits"),
                    ("output_bytes", "1023", "limits"),
                    ("tool_budget", "0", "limits"),
                    ("memory", "0g", "limits"),
                    ("cpus", "inf", "limits"),
                    ("pids", "1.5", "limits"),
                )
                for key, invalid, page in cases:
                    with self.subTest(key=key):
                        field = screen.query_one(f"#env-{key}", Input)
                        original = field.value
                        field.value = invalid
                        await click(pilot, app, "#config-nav-overview")
                        await click(pilot, app, "#environment-apply")
                        self.assertIs(app.screen, screen)
                        self.assertEqual(screen.query_one(ContentSwitcher).current, f"config-page-{page}")
                        self.assertIs(screen.focused, field)
                        self.assertTrue(screen.query_one("#environment-result", Static).render())
                        self.assertFalse(screen.query_one("#environment-apply", Button).disabled)
                        field.value = original
                self.assertIsNone(app.session)
                self.assertEqual(app.history.list_paths(), [])

    async def test_keyboard_navigation_and_shared_footer_at_80_by_24(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                screen = app.screen
                for page in screen.PAGES:
                    button = screen.query_one(f"#config-nav-{page}", Button)
                    button.scroll_visible(animate=False)
                    button.focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(screen.query_one(ContentSwitcher).current, f"config-page-{page}")
                    self.assertTrue(button.has_class("active-page"))
                    self.assertEqual(len(screen.query(".active-page")), 1)
                    self.assertGreater(screen.query_one(ContentSwitcher).region.height, 0)
                    for identifier in ("#environment-apply", "#environment-back"):
                        footer = screen.query_one(identifier, Button)
                        self.assertGreaterEqual(footer.region.y, 0)
                        self.assertLessEqual(footer.region.bottom, 24)
                        self.assertLessEqual(footer.region.right, 80)
                await pilot.press("tab")
                self.assertIsNotNone(screen.focused)
                await click(pilot, app, "#config-nav-limits")
                screen.query_one("#env-tool_budget", Input).value = "0"
                await click(pilot, app, "#environment-apply")
                self.assertIs(screen.focused, screen.query_one("#env-tool_budget", Input))
                self.assertLessEqual(screen.focused.region.bottom, 24)
                await pilot.press("escape")
                self.assertIsInstance(app.screen, EnvironmentMenu)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, MainMenu)
                self.assertIsNone(app.session)

    async def test_startup_environment_first_and_return_preserves_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, MainMenu)
                self.assertIsNone(app.session)
                self.assertEqual(app.history.list_paths(), [])
                await pilot.press("ctrl+enter", "ctrl+n", "escape")
                self.assertIsInstance(app.screen, MainMenu)
                await self.create(pilot, app)
                self.assertEqual(app.session.model, "model:two")
                self.assertIsNotNone(app.session.environment)
                self.assertEqual(app.session.schema_version, 5)
                self.assertEqual(Path(app.session.workspace), app.storage.workspace(app.session.session_id))
                self.assertFalse(Path(app.session.workspace).is_relative_to(Path.cwd()))
                self.assertEqual(len(app.history.list_paths()), 1)
                self.assertTrue(app.models["model:two"].tools)
                original = app.session.session_id
                app.query_one("#composer", TextArea).load_text("Keep my draft")
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-back")
                self.assertEqual(app.session.session_id, original)
                self.assertEqual(app.query_one("#composer", TextArea).text, "Keep my draft")
                self.assertEqual(len(app.query("#workspace, #image, #sidebar")), 0)
                self.assertIn(app.session.workspace, str(app.query_one("#conversation-summary", Static).render()))

    async def test_cancel_new_draft_keeps_active_settings_and_uses_launch_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                original = app.session
                await app.apply_environment(replace(app.settings, tool_budget=7, image="custom:latest"),
                                            Path(app.session.workspace))
                app.query_one("#composer", TextArea).load_text("Draft")
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                self.assertEqual(app.screen.query_one("#env-tool_budget", Input).value, "20")
                self.assertEqual(app.screen.query_one("#env-image", Input).value, app.launch_settings.image)
                app.screen.query_one("#env-image", Input).value = "cancelled:latest"
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.session.session_id, original.session_id)
                self.assertEqual(app.settings.tool_budget, 7)
                self.assertEqual(app.query_one("#composer", TextArea).text, "Draft")
                self.assertEqual(len(app.history.list_paths()), 1)

    async def test_resume_legacy_recovers_and_corrupt_history_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            saved = Session("model:two", app.settings.ollama_url, directory, state="running")
            saved.messages = [Message("assistant", "Partial", status="streaming", tool_calls=[
                {"function": {"name": "terminal", "arguments": {"command": "echo hi", "cwd": "."}}},
            ])]
            save_managed(app, saved)
            broken_id = "f" * 32
            app.storage.create(broken_id, workspace=False)
            app.history.path_for(broken_id).write_text("{broken")
            app.sandbox.execute = AsyncMock()
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-histories")
                highlight_history(app, broken_id)
                await click(pilot, app, "#open-highlighted")
                self.assertIn("Cannot load", str(app.screen.query_one("#history-result", Static).render()))
                self.assertIsNone(app.session)
                highlight_history(app, saved.session_id)
                await click(pilot, app, "#open-highlighted")
                self.assertEqual(app.session.state, "interrupted")
                self.assertEqual(app.session.messages[-1].role, "tool")
                self.assertEqual(app.session.schema_version, 5)
                app.sandbox.execute.assert_not_awaited()

    async def test_edit_validation_confirm_deny_and_saved_provider_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                app.session.messages = [Message("user", "Existing context")]
                app.history.save(app.session)
                original = app.settings
                old_provider = app.provider
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-environment")
                app.screen.query_one("#env-command_timeout", Input).value = "0"
                await click(pilot, app, "#environment-apply")
                self.assertEqual(app.settings, original)
                self.assertIn("positive", str(app.screen.query_one("#environment-result", Static).render()))
                for key, value in {"command_timeout": "90",
                                   "output_bytes": "2048", "tool_budget": "7",
                                   "image": "custom:latest", "memory": "2g", "cpus": "3", "pids": "80"}.items():
                    app.screen.query_one(f"#env-{key}", Input).value = value
                app.screen.query_one("#env-runtime", Select).value = "podman"
                await click(pilot, app, "#environment-apply")
                old_provider.close.assert_not_awaited()
                self.assertEqual(app.settings.ollama_url, original.ollama_url)
                self.assertEqual(app.settings.runtime, "podman")
                self.assertEqual(app.sandbox.settings.command_timeout, 90)
                self.assertEqual(app.sandbox.settings.memory, "2g")
                self.assertEqual(app.agent.budget, 7)
                self.assertEqual(app.session.model, "model:two")
                self.assertEqual(app.session.messages[0].content, "Existing context")
                saved = app.history.load(app.history.path_for(app.session.session_id))
                self.assertEqual(saved.environment, app.session.environment)

    async def test_edit_blocks_attached_sandbox_and_persistence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                app.sandbox.container_id = "existing"
                app.sandbox.workspace = root
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-environment")
                self.assertTrue(app.screen.query_one("#env-command_timeout", Input).disabled)
                self.assertTrue(app.screen.query_one("#environment-apply", Button).disabled)
                self.assertIn("DISCONNECT", str(app.screen.query_one("#config-sandbox-status", Static).render()))
                app.sandbox.detach()
                app.screen.refresh_sandbox_controls()
                app.screen.query_one("#env-command_timeout", Input).value = "90"
                original = app.session
                with patch.object(app.history, "save", side_effect=HistoryError("Permission denied")):
                    await click(pilot, app, "#environment-apply")
                self.assertIs(app.session, original)
                self.assertEqual(app.settings.command_timeout, 60)
                self.assertIn("Permission denied", str(app.screen.query_one("#environment-result", Static).render()))

    async def test_sidebar_cleanup_configuration_create_and_retained_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                for identifier in ("delete-history", "manage-history", "create", "attach", "container"):
                    self.assertEqual(len(app.query(f"#{identifier}")), 0)
                self.assertEqual(len(app.query("#disconnect")), 0)
                await pilot.press("f2")
                await pilot.pause()
                self.assertEqual(len(app.screen.query("#disconnect")), 1)
                await click(pilot, app, "#menu-environment")
                self.assertEqual(len(app.screen.query("#config-create")), 1)
                self.assertEqual(len(app.screen.query("#config-attach")), 1)
                app.sandbox.pull = AsyncMock()

                async def create(workspace, image):
                    app.sandbox.container_id, app.sandbox.workspace, app.sandbox.owned = "new-container", workspace, True

                app.sandbox.create = AsyncMock(side_effect=create)
                app.screen.query_one("#env-image", Input).value = "unsaved:image"
                await click(pilot, app, "#config-create")
                self.assertIn("Save environment changes", str(app.screen.query_one("#environment-result", Static).render()))
                app.sandbox.pull.assert_not_awaited()
                app.screen.query_one("#env-image", Input).value = app.settings.image
                await click(pilot, app, "#config-create")
                await pilot.press("escape")
                await pilot.pause()
                app.sandbox.pull.assert_not_awaited()
                await click(pilot, app, "#config-create")
                await click(pilot, app, "#allow")
                app.sandbox.pull.assert_awaited_once_with(app.settings.image)
                app.sandbox.create.assert_awaited_once_with(Path(app.session.workspace), app.settings.image)
                self.assertEqual(app.session.container_id, "new-container")
                self.assertTrue(app.screen.query_one("#config-attach", Button).disabled)
                await pilot.press("escape")
                await pilot.pause()
                await click(pilot, app, "#menu-back")
                app.sandbox.remove_owned = AsyncMock(side_effect=app.sandbox.detach)
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#disconnect")
                await pilot.pause()
                await click(pilot, app, "#allow")
                app.sandbox.remove_owned.assert_awaited_once()
                self.assertIsNone(app.sandbox.container_id)

    async def test_configuration_attach_reports_failure_and_supports_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-environment")
                app.screen.query_one("#config-container", Input).value = "saved-container"
                app.sandbox.attach = AsyncMock(side_effect=SandboxError("Unsafe container"))
                await click(pilot, app, "#config-attach")
                await click(pilot, app, "#allow")
                self.assertIn("Unsafe container", str(app.screen.query_one("#environment-result", Static).render()))
                self.assertFalse(app.screen.query_one("#config-attach", Button).disabled)
                self.assertIsNone(app.setup_task)

                async def attach(container, workspace):
                    app.sandbox.container_id, app.sandbox.workspace = container, workspace

                app.sandbox.attach = AsyncMock(side_effect=attach)
                await click(pilot, app, "#config-attach")
                await click(pilot, app, "#allow")
                app.sandbox.attach.assert_awaited_once_with("saved-container", Path(app.session.workspace))
                self.assertEqual(app.session.container_id, "saved-container")
                self.assertFalse(app.sandbox.owned)

    async def test_offline_menu_can_configure_new_chat_help_and_histories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            app.provider.failure = True
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.assertFalse(app.screen.query_one("#menu-new", Button).disabled)
                self.assertIn("offline", str(app.screen.query_one("#menu-connection-status", Static).render()))
                await pilot.press("f1")
                await pilot.pause()
                self.assertIsInstance(app.screen, HelpMenu)
                await pilot.press("escape")
                await pilot.pause()
                await click(pilot, app, "#menu-histories")
                self.assertIsInstance(app.screen, HistoryScreen)
                await pilot.press("ctrl+enter", "escape")
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                self.assertIsInstance(app.screen, EnvironmentMenu)
                app.screen.query_one("#env-image", Input).value = "other:latest"
                await click(pilot, app, "#environment-apply")
                self.assertIsInstance(app.screen, ConversationMenu)
                self.assertIsNone(app.session)
                self.assertEqual(app.history.list_paths(), [])

    async def test_empty_resume_and_quit_no_histories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-histories")
                self.assertTrue(app.screen.query_one("#open-highlighted", Button).disabled)
                await pilot.press("escape")
                await pilot.pause()
                await click(pilot, app, "#menu-quit")
            self.assertEqual(app.history.list_paths(), [])

    async def test_creation_is_main_menu_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await self.create(pilot, app)
                original = app.session
                self.assertEqual(len(app.query("#new, #refresh")), 0)
                await pilot.press("ctrl+n")
                await pilot.pause()
                self.assertIs(app.session, original)
                self.assertNotIsInstance(app.screen, EnvironmentMenu)
                await pilot.press("f2")
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                await pilot.pause()
                self.assertIsInstance(app.screen, EnvironmentMenu)

    async def test_two_environments_restore_across_switch_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_a, work_b = root / "a", root / "b"
            work_a.mkdir()
            work_b.mkdir()
            app = self.app(root)
            env_a = ConversationEnvironment.from_settings(replace(app.launch_settings, ollama_url="http://a:11434", tool_budget=4), work_a)
            env_b = ConversationEnvironment.from_settings(replace(app.launch_settings, ollama_url="http://b:11434", runtime="podman",
                                                                 image="other:image", command_timeout=99, output_bytes=2048,
                                                                 request_timeout=55, tool_budget=9, memory="2g", cpus=3, pids=70), work_b)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                self.assertTrue(await app.create_session("model:one", env_a))
                a_id = app.session.session_id
                self.assertTrue(await app.create_session("model:two", env_b))
                b_id = app.session.session_id
                await app.load_session(app.history.path_for(a_id))
                self.assertEqual(app.settings.ollama_url, app.launch_settings.ollama_url)
                self.assertEqual(app.agent.budget, 4)
                await app.load_session(app.history.path_for(b_id))
                self.assertEqual(app.sandbox.settings.runtime, "podman")
                self.assertEqual(app.settings.image, env_b.image)
                self.assertEqual(app.session.workspace, str(app.storage.workspace(b_id)))
                self.assertIsNone(app.sandbox.container_id)
            restarted = self.app(root)
            async with restarted.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await restarted.load_session(restarted.history.path_for(b_id))
                self.assertEqual(restarted.session.environment, replace(env_b, workspace=str(restarted.storage.workspace(b_id))))
                self.assertEqual(restarted.agent.budget, 9)
                self.assertEqual(restarted.provider.url, restarted.launch_settings.ollama_url)
                self.assertEqual(restarted.settings.command_timeout, 99)

    async def test_switch_sandbox_cancel_leave_and_old_runtime_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                self.assertTrue(await app.create_session("model:one"))
                old_session = app.session
                env = ConversationEnvironment.from_settings(replace(app.launch_settings, runtime="podman"), root)
                saved = Session("model:two", app.settings.ollama_url, directory)
                saved.set_environment(env)
                save_managed(app, saved)
                path = app.history.path_for(saved.session_id)
                app.sandbox.container_id = "owned"
                app.sandbox.workspace, app.sandbox.owned, app.sandbox.healthy = root, True, True
                task = asyncio.create_task(app.load_session(path))
                await pilot.pause()
                self.assertIsInstance(app.screen, SandboxSwitch)
                await click(pilot, app, "#switch-cancel")
                with self.assertRaisesRegex(ValueError, "cancelled"):
                    await task
                self.assertIs(app.session, old_session)
                self.assertEqual(app.sandbox.container_id, "owned")
                task = asyncio.create_task(app.load_session(path))
                await pilot.pause()
                await click(pilot, app, "#switch-leave")
                await task
                self.assertIsNone(app.sandbox.container_id)
                self.assertEqual(app.settings.runtime, "podman")
                self.assertEqual(app.history.load(app.history.path_for(old_session.session_id)).container_id, "owned")
                app.sandbox.container_id = "owned-two"
                app.sandbox.workspace, app.sandbox.owned = root, True
                seen = []

                async def remove():
                    seen.append(app.sandbox.settings.runtime)
                    app.sandbox.detach()

                a = app.history.path_for(old_session.session_id)
                with patch.object(app.sandbox, "remove_owned", side_effect=remove) as removal:
                    task = asyncio.create_task(app.load_session(a))
                    await pilot.pause()
                    await click(pilot, app, "#switch-remove")
                    await task
                removal.assert_awaited_once()
                self.assertEqual(seen, ["podman"])
                self.assertEqual(app.settings.runtime, "docker")
                self.assertIsNone(app.sandbox.container_id)

    async def test_attached_container_cannot_be_removed_and_remove_failure_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await app.create_session("model:one")
                original = app.session
                target = Session("model:two", app.settings.ollama_url, directory)
                target.set_environment(ConversationEnvironment.from_settings(app.settings, root))
                save_managed(app, target)
                app.sandbox.container_id, app.sandbox.workspace = "attached", root
                task = asyncio.create_task(app.load_session(app.history.path_for(target.session_id)))
                await pilot.pause()
                self.assertEqual(len(app.screen.query("#switch-remove")), 0)
                await click(pilot, app, "#switch-cancel")
                with self.assertRaises(ValueError):
                    await task
                app.sandbox.owned = True
                with patch.object(app.sandbox, "remove_owned", side_effect=SandboxError("Removal failed")):
                    task = asyncio.create_task(app.load_session(app.history.path_for(target.session_id)))
                    await pilot.pause()
                    await click(pilot, app, "#switch-remove")
                    with self.assertRaises(SandboxError):
                        await task
                self.assertIs(app.session, original)
                self.assertEqual(app.sandbox.container_id, "attached")

    async def test_missing_workspace_and_model_still_viewable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            saved = Session("unavailable", "http://saved-host:11434", str(Path(directory) / "missing"))
            saved.set_environment(ConversationEnvironment.from_settings(replace(app.launch_settings, ollama_url=saved.endpoint),
                                                                       Path(saved.workspace)))
            saved.messages = [Message("assistant", "Saved answer")]
            save_managed(app, saved)
            app.storage.workspace(saved.session_id).rmdir()
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await click(pilot, app, "#menu-histories")
                highlight_history(app, saved.session_id)
                await click(pilot, app, "#open-highlighted")
                await click(pilot, app, "#allow")
                self.assertEqual(app.settings.ollama_url, app.launch_settings.ollama_url)
                self.assertEqual(app.session.session_id, saved.session_id)
                app.query_one("#composer", TextArea).load_text("Don't send")
                await pilot.press("ctrl+enter")
                await pilot.pause()
                self.assertEqual(len(app.session.messages), 1)
                self.assertIn("missing", str(app.query_one("#status", Static).render()))

    async def test_stale_capabilities_do_not_mutate_new_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory))
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                started, finish = asyncio.Event(), asyncio.Event()

                async def slow(name):
                    started.set()
                    await finish.wait()
                    return ModelInfo(name, tools=True)

                app.provider.capabilities = slow
                task = asyncio.create_task(app.discover_capabilities("old:model"))
                await started.wait()
                app.approve = AsyncMock(return_value=True)
                await app.apply_connection("http://new:11434", 1200)
                finish.set()
                self.assertFalse(await task)
                self.assertNotIn("old:model", app.models)

    async def test_failed_new_persistence_does_not_switch_or_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                await app.create_session("model:one")
                original, provider = app.session, app.provider
                app.sandbox.container_id, app.sandbox.workspace, app.sandbox.owned = "keep", root, True
                app.sandbox.remove_owned = AsyncMock()
                env = ConversationEnvironment.from_settings(replace(app.launch_settings, ollama_url="http://other:11434"), root)
                with patch.object(app.history, "save", side_effect=HistoryError("Cannot save")):
                    task = asyncio.create_task(app.create_session("model:two", env))
                    await pilot.pause()
                    await click(pilot, app, "#switch-leave")
                    self.assertFalse(await task)
                self.assertIs(app.session, original)
                self.assertIs(app.provider, provider)
                self.assertEqual(app.sandbox.container_id, "keep")
                app.sandbox.remove_owned.assert_not_awaited()
                self.assertEqual(len(app.history.list_paths()), 1)

    async def test_slow_startup_discovery_cancelled_when_draft_committed(self) -> None:
        class SlowProvider(MenuProvider):
            async def list_models(self):
                started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.set()
                return []

        with tempfile.TemporaryDirectory() as directory:
            started, cancelled = asyncio.Event(), asyncio.Event()
            app = self.app(Path(directory))
            app.provider = SlowProvider()
            app.agent.provider = app.provider
            async with app.run_test(size=(120, 45)) as pilot:
                await started.wait()
                await pilot.pause()
                await click(pilot, app, "#menu-new")
                app.screen.query_one("#env-image", Input).value = "other:latest"
                await click(pilot, app, "#environment-apply")
                await click(pilot, app, "#conversation-open")
                self.assertTrue(cancelled.is_set())
                self.assertEqual(app.session.endpoint, app.launch_settings.ollama_url)
                self.assertFalse(app.loading)

    async def test_same_provider_workspace_switch_invalidates_late_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "other").mkdir()
            app = self.app(root)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                started, finish = asyncio.Event(), asyncio.Event()
                original = app.provider

                async def slow(name):
                    started.set()
                    await finish.wait()
                    return ModelInfo(name, tools=True)

                app.provider.capabilities = slow
                task = asyncio.create_task(app.discover_capabilities("late:model"))
                await started.wait()
                await app.activate_environment(ConversationEnvironment.from_settings(app.settings, root / "other"))
                finish.set()
                self.assertFalse(await task)
                self.assertIs(app.provider, original)
                self.assertNotIn("late:model", app.models)
