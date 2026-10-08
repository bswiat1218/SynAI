from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from rich.text import Text
from textual.app import App, ComposeResult
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.color import Color
from textual.content import Content
from textual.message import Message as UiMessage
from textual.screen import ModalScreen
from textual.worker import WorkerCancelled
from textual.widgets import Button, Footer, Header, Label, RichLog, SelectionList, Static, TextArea

from synai.agent import Agent
from synai.config import ConversationEnvironment, Settings
from synai.history import History, HistoryError, ManagedHistory
from synai.models import Activity, ModelInfo, Session
from synai.providers.base import ProviderError
from synai.providers.ollama import OllamaProvider
from synai.preferences import DEFAULT_THEME, Preferences, PreferencesStore
from synai.sandbox import Sandbox, SandboxError
from synai.host import HostExecution
from synai.storage import ConversationStorage
from synai.tools import Tools
from synai.tui.activity import format_activity
from synai.tui.menu import ConnectionMenu, ConversationMenu, EnvironmentMenu, HelpMenu, MainMenu, SandboxSwitch
from synai.tui.navigation import MenuBody, MenuScreen
from synai.tui.palette import CYBERPUNK, rich_theme
from synai.tui.theme_picker import ThemePicker
from synai.execution_backend import validate_workspace
from synai.editor.environment import EditorContext
from synai.editor.manager import EditorManager
from synai.editor.protocol import COLORS, EditorError


class ApprovalScreen(MenuScreen[bool]):
    BINDINGS = [("escape", "deny", "Deny")]

    def __init__(self, title: str, details: str, confirm_label: str = "ALLOW ONCE", destructive: bool = False) -> None:
        super().__init__()
        self.title_text, self.details = title, details
        self.confirm_label, self.destructive = confirm_label, destructive

    def compose(self) -> ComposeResult:
        with Vertical(id="approval", classes="modal-shell"):
            yield Label(self.title_text, id="approval-title")
            with MenuBody(classes="modal-body"):
                yield Static(Text(self.details))
            with Horizontal(classes="modal-actions"):
                yield Button(self.confirm_label, id="allow", variant="error" if self.destructive else "warning")
                yield Button("DENY", id="deny")
            yield Static("Enter: choose | Esc: deny | PgUp/PgDn: review", classes="menu-hints")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_deny(self) -> None:
        self.dismiss(False)

    def on_mount(self) -> None:
        self.query_one("#deny", Button).focus()


class ConversationList(SelectionList[str]):
    BINDINGS = [Binding("enter", "open_highlighted", "Open conversation", show=False)]

    class OpenHighlighted(UiMessage):
        pass

    def action_open_highlighted(self) -> None:
        if not self.disabled:
            self.post_message(self.OpenHighlighted())


class HistoryScreen(MenuScreen[None]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, app: CodingApp, menu: MainMenu | None = None) -> None:
        super().__init__()
        self.coding_app = app
        self.paths: dict[str, Path] = {}
        self.deleting = False
        self.opening = False
        self.menu = menu

    def compose(self) -> ComposeResult:
        with Vertical(id="history-manager", classes="modal-shell"):
            yield Label("CONVERSATIONS", classes="modal-title")
            yield Static("Arrows highlight; Enter opens. Space / click checks rows for deletion. "
                         "Deleting permanently removes their managed workspace files.")
            yield ConversationList(id="history-selection")
            yield Static("Highlight a conversation to open, or check rows to delete.", id="history-result")
            with Horizontal(id="history-actions", classes="modal-actions"):
                yield Button("OPEN", id="open-highlighted", variant="success", disabled=True)
                yield Button("DELETE SELECTED", id="delete-selected", variant="error", disabled=True)
                yield Button("CLOSE", id="close-history")
            yield Static("Enter: open | Space: check | Esc: close | F1: Help", classes="menu-hints")

    def on_mount(self) -> None:
        self.refresh_entries()
        self.query_one("#history-selection").focus()

    def refresh_entries(self, selected: set[str] | None = None) -> None:
        choices = self.query_one("#history-selection", SelectionList)
        highlighted = (
            choices.get_option_at_index(choices.highlighted).value
            if choices.highlighted is not None else None
        )
        self.paths = {}
        choices.clear_options()
        try:
            entries = self.coding_app.history_entries()
        except OSError as exc:
            self.query_one("#history-result", Static).update(Text(f"Cannot list histories: {exc}", style="synai.error"))
            self.selection_changed()
            return
        self.paths = {self.coding_app.history.identifier(path): path for label, path in entries}
        choices.add_options([(label, self.coding_app.history.identifier(path),
                              self.coding_app.history.identifier(path) in (selected or set())) for label, path in entries])
        if entries:
            identifiers = list(self.paths)
            choices.highlighted = identifiers.index(highlighted) if highlighted in self.paths else 0
        else:
            self.query_one("#history-result", Static).update("No saved conversations yet.")
        self.selection_changed()

    def on_selection_list_selected_changed(self) -> None:
        self.selection_changed()

    def on_selection_list_selection_highlighted(self) -> None:
        self.selection_changed()

    def on_conversation_list_open_highlighted(self, event: ConversationList.OpenHighlighted) -> None:
        event.stop()
        self.start_open()

    def selection_changed(self) -> None:
        choices = self.query_one("#history-selection", SelectionList)
        count = len(choices.selected)
        working = self.deleting or self.opening
        self.query_one("#delete-selected", Button).disabled = count == 0 or working
        self.query_one("#open-highlighted", Button).disabled = choices.highlighted is None or not self.paths or working
        choices.disabled = working
        self.query_one("#close-history", Button).disabled = working

    def start_open(self) -> None:
        if self.deleting or self.opening or self.coding_app.approval_future is not None:
            return
        choices = self.query_one("#history-selection", SelectionList)
        if choices.highlighted is None:
            return
        identifier = choices.get_option_at_index(choices.highlighted).value
        path = self.paths.get(identifier)
        if path is None:
            return
        self.opening = True
        self.selection_changed()
        self.run_worker(self.open_highlighted(path), exclusive=True, group="history-open")

    async def open_highlighted(self, path: Path) -> None:
        app = self.coding_app
        try:
            await app.load_session(path)
            self.dismiss(None)
            if self.menu is not None and app.screen is self.menu:
                self.menu.dismiss(None)
            app.controls()
            app.query_one("#composer").focus()
        except (HistoryError, ValueError, SandboxError, OSError, TimeoutError) as exc:
            self.query_one("#history-result", Static).update(Text(str(exc), style="synai.error"))
        finally:
            self.opening = False
            if self.is_mounted:
                self.selection_changed()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "close-history":
            self.action_close()
        elif event.button.id == "open-highlighted":
            self.start_open()
        elif event.button.id == "delete-selected" and not self.deleting and not self.opening:
            self.deleting = True
            self.selection_changed()
            self.query_one("#history-selection").disabled = True
            self.query_one("#close-history").disabled = True
            self.run_worker(self.delete_selected(), exclusive=True, group="history-delete")

    async def delete_selected(self) -> None:
        try:
            selected = set(self.query_one("#history-selection", SelectionList).selected)
            paths = [path for key, path in self.paths.items() if key in selected]
            deleted, errors = await self.coding_app.delete_histories(paths)
            if deleted or errors:
                self.refresh_entries(selected - {self.coding_app.history.identifier(path) for path in deleted})
                report = f"Deleted {len(deleted)} of {len(paths)} selected chats."
                if errors:
                    report += "\n" + "\n".join(errors)
                self.query_one("#history-result", Static).update(Text(report, style="synai.error" if errors else "synai.primary"))
        finally:
            self.deleting = False
            if self.is_mounted:
                self.query_one("#history-selection").disabled = False
                self.query_one("#close-history").disabled = False
                self.selection_changed()

    def action_close(self) -> None:
        if not self.deleting and not self.opening and self.coding_app.approval_future is None:
            self.dismiss(None)


class CodingApp(App[None]):
    TITLE = "SynAI"
    SUB_TITLE = "LOCAL INTELLIGENCE / HUMAN CONTROL"
    CSS_PATH = "theme.tcss"
    BINDINGS = [
        Binding("ctrl+enter", "send", "Send", priority=True),
        Binding("ctrl+j", "send", "Send", show=False, priority=True),
        Binding("ctrl+s", "send", "Send (fallback)", priority=True),
        ("escape", "stop", "Stop"),
        ("ctrl+q", "quit_agent", "Quit"),
        ("f2", "main_menu", "Menu"),
        Binding("f1", "help", "Help", priority=True),
        Binding("ctrl+p", "themes", "Themes", priority=True),
        Binding("f3", "focus_pane('composer')", "Prompt", show=False, priority=True),
        Binding("f4", "focus_pane('chat')", "Chat", show=False, priority=True),
        Binding("f5", "focus_pane('thinking')", "Reasoning", show=False, priority=True),
        Binding("f6", "focus_pane('activity')", "Activity", show=False, priority=True),
        Binding("f7", "workspace_editor", "Editor", priority=True),
        Binding("f8", "select_prompt", "Select prompt", show=False),
    ]

    def format_title(self, title: str, sub_title: str) -> Content:
        return Content.assemble((title, "bold"), " / " if sub_title else "", sub_title)

    def on_app_focus(self, event: events.AppFocus) -> None:
        if isinstance(self.screen, MenuScreen):
            self.screen.recover_focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "focus_pane" and isinstance(self.screen, ModalScreen):
            return False
        return super().check_action(action, parameters)

    def __init__(
        self, settings: Settings, *, start_menu: bool = True,
        preferences: Preferences | None = None, connection_sources: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.preferences_store = PreferencesStore(settings.history_dir)
        self.preferences = preferences if preferences is not None else self.preferences_store.load()
        self.connection_sources = dict(connection_sources or {})
        self.register_theme(CYBERPUNK)
        self.theme_warning = None
        if self.preferences.theme not in self.available_themes:
            self.theme_warning = f"Saved theme {self.preferences.theme!r} is unavailable; using SynAI cyberpunk."
        self.theme = self.preferences.theme if self.preferences.theme in self.available_themes else DEFAULT_THEME
        self.rich_theme_installed = False
        self.theme_preview_original: str | None = None
        self.theme_picker_open = False
        self.transient_notes: list[tuple[str, bool]] = []
        self.settings = settings
        self.launch_settings = settings
        self.launch_workspace = Path.cwd().resolve()
        self.provider = OllamaProvider(settings.ollama_url, settings.request_timeout)
        self.storage = ConversationStorage(settings.history_dir)
        self.history = ManagedHistory(self.storage, settings)
        self.sandbox = Sandbox(settings)
        self.host = HostExecution(settings)
        self.host_error: str | None = None
        self.session: Session | None = None
        self.models: dict[str, ModelInfo] = {}
        self.turn_task: asyncio.Task[None] | None = None
        self.setup_task: asyncio.Task[None] | None = None
        self.approval_future: asyncio.Future[bool] | None = None
        self.history_paths: dict[str, Path] = {}
        self.agent = Agent(self.provider, self.history, Tools(self.sandbox, self.approve), self.render_session, settings.tool_budget)
        self.agent.connection_endpoint = settings.ollama_url
        self.loading = False
        self.render_dirty = False
        self.shutting_down = False
        self.history_manager_open = False
        self.start_menu = start_menu
        self.connection_error: str | None = None
        self.switching = False
        self.environment_epoch = 0
        self.legacy_report = ""
        self.legacy_retry = False
        self.storage_error: str | None = None
        self.help_open = False
        self.editor = EditorManager(self.note)
        self.editor_launching = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("▓▒░  SynAI  ░▒▓ // F2 MENU // F3 PROMPT · F4 CHAT · F5 REASONING · F6 ACTIVITY", id="neon-banner")
        yield Static("No conversation selected // F2 to begin", id="conversation-summary")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Label("01 // CONVERSATION LINK", classes="pane-title")
                yield RichLog(id="chat", min_width=1, wrap=True, markup=False, highlight=False, auto_scroll=True)
                yield Label("PROMPT // CTRL+ENTER / CTRL+S", classes="pane-title")
                yield TextArea(id="composer", soft_wrap=True)
                with Horizontal(id="buttons", classes="modal-actions"):
                    yield Button("SEND", id="send", variant="success")
                    yield Button("STOP", id="stop", variant="error")
            with Vertical(id="activity-panel"):
                yield Label("02 // REASONING SIGNAL", classes="pane-title")
                yield RichLog(id="thinking", min_width=1, wrap=True, markup=False, highlight=False)
                yield Label("03 // TOOL ACTIVITY", classes="pane-title")
                yield RichLog(id="activity", min_width=1, wrap=True, markup=False, highlight=False)
        yield Static("Ready", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.theme_changed_signal.subscribe(self, self.apply_theme)
        self.apply_theme(persist=False)
        try:
            self.storage.initialize()
        except (ValueError, OSError) as exc:
            self.storage_error = f"Storage unavailable: {exc}"
            self.note(self.storage_error, True)
        self.set_interval(0.1, self.flush_render)
        self.refresh_history()
        if self.start_menu:
            self.action_main_menu()
        if self.theme_warning:
            self.note(self.theme_warning, True)
        self.run_worker(self.refresh_models(), exclusive=True, group="models")
        if self.start_menu and self.storage_error is None:
            self.run_worker(self.offer_legacy_cleanup(), exclusive=True, group="legacy-cleanup")

    async def on_unmount(self) -> None:
        self.shutting_down = True
        await self.editor.parent_shutdown()
        self.host.revoke()
        for task in (self.turn_task, self.setup_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.provider.close()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 105, "narrow")
        self.set_class(self.size.height < 36, "compact")
        self.render_dirty = True

    def note(self, text: str, error: bool = False) -> None:
        if self.shutting_down or not self.is_running:
            return
        self.transient_notes.append((text, error))
        self.transient_notes = self.transient_notes[-100:]
        self.query_one("#status", Static).update(Text(text, style="synai.error" if error else "synai.primary"))
        self.query_one("#activity", RichLog).write(Text(text, style="synai.error" if error else "synai.primary"))

    def apply_theme(self, _theme: object = None, *, persist: bool = True) -> None:
        if self.shutting_down:
            return
        if self.rich_theme_installed:
            self.console.pop_theme()
        self.console.push_theme(rich_theme(self.get_css_variables()))
        self.rich_theme_installed = True
        if self.editor.active:
            self.run_worker(self.sync_editor_theme(), group="editor-theme")
        scroll = {identifier: self.query_one(f"#{identifier}", RichLog).scroll_y
                  for identifier in ("chat", "thinking", "activity")}
        status = self.query_one("#status", Static).render()
        notes = list(self.transient_notes)
        self.render_dirty = True
        self.flush_render()
        if self.session is None:
            self.query_one("#activity", RichLog).clear()
        for text, error in notes:
            self.query_one("#activity", RichLog).write(Text(text, style="synai.error" if error else "synai.primary"))
        self.transient_notes = notes
        self.query_one("#status", Static).update(status)
        for identifier, offset in scroll.items():
            self.query_one(f"#{identifier}", RichLog).scroll_to(y=offset, animate=False, force=True)
        for screen in self.screen_stack:
            screen.refresh(repaint=True)
            for widget in screen.query(Static):
                widget.refresh(repaint=True)
        if persist and self.theme_preview_original is None and self.theme != self.preferences.theme:
            updated = replace(self.preferences, theme=self.theme)
            try:
                self.preferences_store.save(updated)
            except (ValueError, OSError) as exc:
                self.note(f"Theme applied but not saved: {exc}", True)
            else:
                self.preferences = updated

    def save_selected_theme(self, name: str) -> None:
        if name not in self.available_themes:
            raise ValueError(f"Theme {name!r} is unavailable.")
        updated = replace(self.preferences, theme=name)
        self.preferences_store.save(updated)
        self.preferences = updated
        self.theme = name

    def finish_theme_preview(self, committed: bool) -> None:
        original = self.theme_preview_original
        if original is None:
            return
        if not committed and not self.shutting_down:
            self.theme = original

        def finished() -> None:
            self.theme_preview_original = None
            self.theme_picker_open = False

        # Keep persistence suppressed until queued theme notifications have repainted.
        self.call_after_refresh(finished)

    def action_themes(self) -> None:
        if self.shutting_down or self.theme_picker_open or self.approval_future is not None or isinstance(
            self.screen, (ApprovalScreen, SandboxSwitch, HelpMenu),
        ):
            return
        owner = self.screen
        if self.switching or (self.setup_task and not self.setup_task.done()) or (
            isinstance(owner, EnvironmentMenu) and (owner.applying or owner.sandbox_working)
        ) or (isinstance(owner, ConnectionMenu) and owner.saving) or (
            isinstance(owner, (ConversationMenu, HistoryScreen)) and owner.opening
        ) or (isinstance(owner, HistoryScreen) and owner.deleting):
            self.note("Wait for the current menu operation before opening Themes.")
            return
        previous_focus = owner.focused
        self.theme_preview_original = self.theme
        self.theme_picker_open = True

        def closed(value: bool) -> None:
            self.controls()
            if self.screen is owner and previous_focus in owner.focus_chain:
                owner.set_focus(previous_focus, scroll_visible=False)

        self.push_screen(ThemePicker(self), closed)
        self.controls()

    def operation_busy(self) -> bool:
        if self.shutting_down or not self.is_running or not self.screen_stack:
            return True
        return bool(
            (self.turn_task and not self.turn_task.done())
            or (self.setup_task and not self.setup_task.done())
            or self.approval_future is not None
            or self.history_manager_open
            or self.switching
            or self.editor_launching or self.editor.starting or self.editor.closing
        )

    def editor_palette(self) -> dict[str, object]:
        variables = self.get_css_variables()
        return {
            "dark": self.available_themes[self.theme].dark,
            **{key: Color.parse(variables[key]).hex[:7] for key in COLORS},
        }

    def guard_editor_context(self) -> None:
        if self.editor_launching:
            raise ValueError("Wait for workspace editor launch before changing conversation or execution environment")
        self.editor.guard_context()

    async def sync_editor_theme(self) -> None:
        try:
            await self.editor.set_theme(self.editor_palette())
        except (EditorError, OSError, TimeoutError) as exc:
            self.note(f"Editor theme update failed: {exc}", True)

    def action_workspace_editor(self) -> None:
        if not self.dashboard_idle():
            self.note("Finish the current operation or close the overlay before opening the editor", True)
            return
        self.editor_launching = True
        self.run_worker(self.open_workspace_editor(), group="editor-launch")

    def action_select_prompt(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        composer = self.query_one("#composer", TextArea)
        if self.screen.focused is composer:
            composer.select_all()

    async def open_workspace_editor(self) -> None:
        try:
            if self.session is None or self.session.environment is None:
                raise EditorError("Create or open a conversation before using the F7 workspace editor")
            session = self.session
            settings = session.environment.settings(self.launch_settings)
            sandbox = settings.execution_mode == "sandbox"
            workspace = validate_workspace(Path(session.workspace), settings, sandbox=sandbox)
            if sandbox:
                if not self.sandbox.matches(session):
                    raise EditorError("Attach a validated sandbox for this conversation in F2 Settings before opening the editor")
                await self.sandbox.validate()
            context = EditorContext(
                session_id=session.session_id, workspace=str(workspace),
                mode=settings.execution_mode,
                runtime=settings.runtime if sandbox else "",
                container=(self.sandbox.container_id or "") if sandbox else "",
                uid=self.sandbox.uid if sandbox else os.getuid(),
                mini_path=str(Path(os.environ["SYNAI_MINI_PATH"]).expanduser().resolve())
                if not sandbox and os.environ.get("SYNAI_MINI_PATH") else "",
                shell="/bin/sh" if sandbox else os.environ.get("SHELL", "/bin/sh"),
            )
            if not self.editor.active:
                label = "SANDBOX" if sandbox else "HOST // NOT ISOLATED"
                if not await self.approve(
                    f"Open workspace editor // {label}?",
                    f"Workspace: {workspace}\n"
                    "Neovim and its interactive terminal run in this conversation's selected environment.\n"
                    "Manual terminal commands and Neovim shell commands do NOT have per-command AI approvals. "
                    "Opening the editor does not authorize AI tools.\n"
                    + ("The existing sandbox's limits still apply." if sandbox else
                       "Host programs can access anything your Linux account can access."),
                    confirm_label="OPEN EDITOR",
                ):
                    return
            await self.editor.launch(context, self.editor_palette())
            await self.editor.set_theme(self.editor_palette())
            self.note(f"Workspace editor open // {'SANDBOX' if sandbox else 'HOST // NOT ISOLATED'}")
        except (EditorError, ValueError, OSError, SandboxError, TimeoutError) as exc:
            self.note(f"Workspace editor: {exc}", True)
        finally:
            self.editor_launching = False
            self.controls()

    def busy(self) -> bool:
        return self.operation_busy() or isinstance(self.screen, ModalScreen)

    def dashboard_idle(self) -> bool:
        return not self.operation_busy() and (
            not isinstance(self.screen, ModalScreen) or isinstance(self.screen, MainMenu)
        )

    def capability_status(self) -> str:
        model = self.models.get(self.session.model) if self.session else None
        if model is None:
            return f"Saved model {self.session.model!r} unavailable on this connection" if self.session else "No conversation selected"
        return (
            f"{'Native tools' if model.tools else 'Chat only'} // "
            f"{'Reasoning capable' if model.thinking else 'Reasoning not advertised'}"
        )

    def update_summary(self) -> None:
        if self.shutting_down or not self.is_running:
            return
        session = self.session
        text = "No conversation selected // F2 to begin" if session is None else (
            f"{session.model}{' (unavailable)' if session.model not in self.models else ''} // "
            f"{' · '.join(self.execution_status().splitlines())}\n{session.workspace}"
        )
        self.query_one("#conversation-summary", Static).update(Text(text, no_wrap=True, overflow="ellipsis"))

    def controls(self) -> None:
        if self.shutting_down or not self.is_running:
            return
        busy = self.busy()
        self.query_one("#send", Button).disabled = (
            busy or self.session is None or self.loading or self.session.model not in self.models
        )
        self.query_one("#stop", Button).disabled = not bool(self.turn_task and not self.turn_task.done())
        self.update_summary()
        if isinstance(self.screen, MainMenu) and self.screen.is_mounted:
            self.screen.update_status()

    async def refresh_models(self) -> None:
        provider, epoch = self.provider, self.environment_epoch
        self.loading = True
        self.connection_error = None
        self.controls()
        try:
            values = await provider.list_models()
            if provider is not self.provider or epoch != self.environment_epoch:
                return
            self.models = {model.name: model for model in values}
            if not values:
                self.connection_error = f"No models listed at {self.settings.ollama_url}"
                self.note(self.connection_error, True)
        except ProviderError as exc:
            if provider is not self.provider or epoch != self.environment_epoch:
                return
            self.models = {}
            self.connection_error = str(exc)
            self.note(str(exc), True)
        finally:
            if provider is self.provider and epoch == self.environment_epoch:
                self.loading = False
                if self.session and self.session.model in self.models:
                    await self.discover_capabilities(self.session.model)
                self.controls()

    async def discover_capabilities(self, name: str) -> bool:
        provider = self.provider
        epoch = self.environment_epoch
        try:
            model = await provider.capabilities(name)
        except ProviderError as exc:
            if provider is not self.provider or epoch != self.environment_epoch:
                return False
            model = ModelInfo(name, capability_error=str(exc))
            self.note(f"Capability discovery failed; chat only: {exc}", True)
        if provider is not self.provider or epoch != self.environment_epoch:
            return False
        self.models[name] = model
        self.update_summary()
        return True

    async def open_saved_history(self, path: Path) -> None:
        try:
            await self.load_session(path)
        except (HistoryError, ValueError, SandboxError, OSError, TimeoutError) as exc:
            self.note(str(exc), True)
            self.controls()

    async def load_session(self, path: Path) -> None:
        session = self.history.load(path)
        if session.endpoint != self.settings.ollama_url and any(
            message.role in {"user", "assistant"} for message in session.messages
        ):
            if not await self.approve(
                "Open conversation on current connection?",
                f"Recorded origin: {session.endpoint}\nCurrent Ollama: {self.settings.ollama_url}\n"
                "Sending will share retained conversation messages with the current server.",
            ):
                raise ValueError("Conversation opening cancelled.")
        legacy = session.environment is None
        session.set_environment(self.history.environment_for(session))
        state_before_recovery = session.state
        task_status_before_recovery = (
            session.agent_checkpoint.task.status if session.agent_checkpoint is not None else None
        )
        Agent.recover(session)
        if session.agent_checkpoint is not None and (
            session.state != state_before_recovery
            or session.agent_checkpoint.task.status != task_status_before_recovery
        ):
            self.history.save(session)
        if not await self.switch_session(session, persist=True):
            raise ValueError("Conversation switch cancelled.")
        notice = "History reopened; environment restored. " + (
            ("Host tools approved for this opening." if self.host.matches(session)
             else self.host_error or "Host access denied; conversation remains chat-only.")
            if session.environment.execution_mode == "host"
            else "Reattach its sandbox before using tools."
        )
        if legacy:
            notice += " Legacy history: missing environment fields initialized from launch defaults."
        if not Path(session.workspace).is_dir():
            notice += " Workspace is missing; edit the conversation environment before sending."
        if self.connection_error:
            notice += f" {self.connection_error}"
        elif session.model not in self.models:
            notice += f" Saved model {session.model!r} is unavailable; sending is disabled."
        self.note(notice, bool(self.connection_error) or session.model not in self.models)

    def action_main_menu(self) -> None:
        if isinstance(self.screen, MainMenu):
            self.screen.action_back()
            return
        if isinstance(self.screen, ModalScreen) or self.shutting_down:
            return
        if self.approval_future is not None or self.switching or self.history_manager_open or (
            self.setup_task and not self.setup_task.done()
        ):
            return
        previous_focus = self.screen.focused
        def closed(value: None) -> None:
            self.controls()
            if previous_focus is not None and previous_focus.is_mounted:
                previous_focus.focus()

        self.push_screen(MainMenu(self), closed)
        self.controls()

    def action_focus_pane(self, identifier: str) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        widget = self.query_one(f"#{identifier}")
        widget.focus()
        widget.scroll_visible(animate=False)

    def action_help(self) -> None:
        if self.shutting_down or self.help_open or self.approval_future is not None or isinstance(
            self.screen, (ApprovalScreen, SandboxSwitch, HelpMenu),
        ):
            return
        owner = self.screen
        if self.switching or (self.setup_task and not self.setup_task.done()) or (
            isinstance(owner, EnvironmentMenu) and (owner.applying or owner.sandbox_working)
        ) or (
            isinstance(owner, ConnectionMenu) and owner.saving
        ) or (
            isinstance(owner, (ConversationMenu, HistoryScreen)) and owner.opening
        ) or (
            isinstance(owner, HistoryScreen) and owner.deleting
        ):
            self.note("Wait for the current menu operation before opening Help.")
            return
        previous_focus = owner.focused
        self.help_open = True

        def closed(value: None) -> None:
            self.help_open = False
            self.controls()
            if self.screen is owner and previous_focus is not None and previous_focus.is_mounted:
                owner.set_focus(previous_focus, scroll_visible=False)

        self.push_screen(HelpMenu(), closed)
        self.controls()

    async def stop_discovery(self) -> None:
        for worker in self.workers.cancel_group(self, "models"):
            try:
                await worker.wait()
            except WorkerCancelled:
                pass

    async def apply_connection(self, url: str, timeout: float) -> bool:
        if self.switching or self.approval_future is not None or (
            self.turn_task and not self.turn_task.done()
        ) or (self.setup_task and not self.setup_task.done()):
            raise ValueError("Wait for the current operation before changing the connection.")
        updated = replace(self.preferences, ollama_url=url.rstrip("/"), request_timeout=timeout)
        updated.validate()
        self.switching = True
        self.controls()
        try:
            if updated.ollama_url != self.settings.ollama_url:
                if not await self.approve(
                    "Change application-wide connection?",
                    f"Ollama: {self.settings.ollama_url} -> {updated.ollama_url}\n"
                    "This affects every conversation. Future sends can share retained messages with "
                    "the new server. Saved model choices and workspaces remain unchanged.",
                ):
                    return False
            replacement = OllamaProvider(updated.ollama_url, updated.request_timeout)
            try:
                self.preferences_store.save(updated)
            except (ValueError, OSError):
                await replacement.close()
                raise
            await self.stop_discovery()
            self.environment_epoch += 1
            old = self.provider
            self.provider = replacement
            self.agent.provider = replacement
            self.agent.connection_endpoint = updated.ollama_url
            self.preferences = updated
            self.settings = replace(self.settings, ollama_url=updated.ollama_url, request_timeout=updated.request_timeout)
            self.launch_settings = replace(
                self.launch_settings, ollama_url=updated.ollama_url, request_timeout=updated.request_timeout,
            )
            self.connection_sources = {"ollama_url": "saved in UI", "request_timeout": "saved in UI"}
            self.models = {}
            self.sandbox.settings = self.settings
            self.host.settings = self.settings
            await old.close()
            await self.refresh_models()
            return True
        finally:
            self.switching = False
            self.controls()

    async def change_model(self, name: str) -> bool:
        if not self.dashboard_idle() or self.session is None or self.loading:
            return False
        session = self.session
        epoch = self.environment_epoch
        self.switching = True
        self.controls()
        try:
            if name not in self.models:
                raise ValueError("Choose an available model.")
            if not await self.approve(
                "Change conversation model?",
                f"Model: {session.model} -> {name}\n"
                "Retained conversation messages will be sent to this model on your next send. "
                "The conversation, files and execution environment stay unchanged.",
            ):
                return False
            if self.session is not session or self.environment_epoch != epoch or name not in self.models:
                raise ValueError("Connection changed; choose the model again.")
            updated = replace(session, model=name)
            self.history.save(updated)
            self.session = updated
            await self.discover_capabilities(name)
            self.refresh_history()
            await self.render_session()
            self.note(f"Conversation model changed to {name}.")
            return True
        except (HistoryError, ValueError, OSError) as exc:
            self.note(f"Model change failed: {exc}", True)
            return False
        finally:
            self.switching = False
            self.controls()

    async def activate_environment(self, environment: ConversationEnvironment) -> None:
        self.guard_editor_context()
        self.environment_epoch += 1
        if self.loading:
            await self.stop_discovery()
            self.loading = False
        settings = environment.settings(self.launch_settings)
        self.settings = settings
        self.sandbox.settings = settings
        self.host.settings = settings
        self.agent.tools.sandbox = self.host if settings.execution_mode == "host" else self.sandbox
        self.agent.budget = settings.tool_budget
        self.update_summary()

    async def handoff_sandbox(self, pending: Session | None = None, *, new: bool = False) -> bool:
        self.guard_editor_context()
        if not self.sandbox.container_id:
            if pending is not None:
                self.history.save(pending)
            return True
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def decided(value: str | None) -> None:
            if not future.done():
                future.set_result(value or "cancel")

        modal = SandboxSwitch(self.sandbox.container_id, self.sandbox.owned)
        self.push_screen(modal, decided)
        try:
            decision = await future
        finally:
            if self.screen is modal:
                self.pop_screen()
        if decision == "cancel":
            return False
        if self.session:
            previous = replace(self.session, container_id=self.sandbox.container_id)
            self.history.save(previous)
        if pending is not None:
            self.history.save(pending)
        try:
            if decision == "remove":
                await self.sandbox.remove_owned()
            else:
                self.sandbox.detach()
        except (SandboxError, OSError, TimeoutError):
            if pending is not None and new:
                self.history.delete(self.history.path_for(pending.session_id))
            raise
        self.update_summary()
        return True

    async def switch_session(
        self, session: Session, *, handed_off: bool = False, persist: bool = False,
    ) -> bool:
        self.guard_editor_context()
        if session.environment is None:
            raise ValueError("Conversation has no saved environment")
        session.environment.validate()
        self.switching = True
        self.controls()
        try:
            if not handed_off and not await self.handoff_sandbox(session if persist else None):
                return False
            await self.activate_environment(session.environment)
            self.host.revoke()
            self.session = session
            if session.model in self.models:
                await self.discover_capabilities(session.model)
            self.query_one("#composer", TextArea).clear()
            await self.render_session()
            self.flush_render()
            if session.environment.execution_mode == "host":
                await self.enable_host_tools()
            self.update_execution_status()
            return True
        finally:
            self.switching = False
            self.controls()

    async def apply_environment(self, settings: Settings, workspace: Path) -> bool:
        self.guard_editor_context()
        if self.session is None:
            raise ValueError("Create a conversation to save its environment.")
        if self.sandbox.container_id:
            raise ValueError("Disconnect the sandbox from the F2 dashboard before changing its environment.")
        environment = ConversationEnvironment.from_settings(settings, workspace)
        if settings.execution_mode == "sandbox":
            managed = self.storage.workspace(self.session.session_id)
            if workspace != managed:
                raise ValueError("Sandbox workspace cannot be changed; use this conversation's managed folder")
        environment.validate()
        changed_context = environment.workspace != self.session.workspace
        changed_execution = (
            self.session.environment is None
            or environment.execution_mode != self.session.environment.execution_mode
            or environment.workspace != self.session.workspace
        )
        if changed_context and any(message.role in {"user", "assistant"} for message in self.session.messages):
            if not await self.approve("Change conversation environment?",
                                      f"Workspace: {self.session.workspace} -> {environment.workspace}\n"
                                      "Prior messages remain; "
                                      "approved tools will access the new workspace."):
                return False
        updated = replace(self.session)
        updated.set_environment(environment)
        if changed_context or changed_execution or settings.runtime != self.settings.runtime:
            updated.container_id = None
        allocated = False
        try:
            if settings.execution_mode == "sandbox" and not workspace.exists():
                if updated.managed_workspace_created:
                    raise ValueError("Managed workspace is missing. Restore its files before enabling the sandbox.")
                self.storage.create(updated.session_id, workspace=True)
                allocated = True
            if settings.execution_mode == "sandbox":
                updated.managed_workspace_created = True
            self.history.save(updated)
        except (HistoryError, OSError, ValueError):
            if allocated and workspace.exists() and not any(workspace.iterdir()):
                workspace.rmdir()
            raise
        self.switching = True
        self.controls()
        try:
            await self.activate_environment(environment)
            if changed_execution:
                self.host.revoke()
            self.session = updated
            if updated.model in self.models:
                await self.discover_capabilities(updated.model)
            else:
                self.connection_error = f"Conversation saved, but model {updated.model!r} is unavailable at this endpoint."
                self.note(self.connection_error, True)
            if changed_execution and environment.execution_mode == "host":
                await self.enable_host_tools()
            self.update_execution_status()
            return True
        finally:
            self.switching = False
            self.controls()

    def history_entries(self) -> list[tuple[str, Path]]:
        entries = []
        for path in self.history.list_paths():
            try:
                session = self.history.load(path)
                label = f"{session.title[:28]} | {session.model} | {session.updated_at[:19]}"
            except HistoryError as exc:
                label = f"UNREADABLE: {path.name} | {exc}"
            entries.append((label, path))
        return entries

    def refresh_history(self) -> None:
        if self.shutting_down or not self.is_running:
            return
        try:
            self.history_paths = {}
            for _, path in self.history_entries():
                identifier = self.history.identifier(path)
                self.history_paths[identifier] = path
        except OSError as exc:
            self.note(f"History listing failed: {exc}", True)

    async def action_new_session(self, preferred_model: str | None = None) -> None:
        if self.busy():
            return
        self.action_main_menu()

    async def create_session(
        self, model_name: str | None = None, environment: ConversationEnvironment | None = None,
        session_id: str | None = None,
    ) -> bool:
        selected = model_name
        if not selected:
            self.note("Select a model first", True)
            return False
        session: Session | None = None
        committed = False
        try:
            self.guard_editor_context()
            session = Session(str(selected), self.settings.ollama_url, "", schema_version=5)
            if session_id is not None:
                self.storage.folder(session_id)
                session.session_id = session_id
            settings = environment.settings(self.launch_settings) if environment else self.settings
            if settings.execution_mode == "sandbox":
                path = self.storage.workspace(session.session_id)
            else:
                path = Path(environment.workspace if environment else self.launch_workspace).expanduser().resolve(strict=True)
                if not path.is_dir():
                    raise ValueError("Workspace must be an existing directory.")
            workspace = str(path)
            environment = ConversationEnvironment.from_settings(settings, path)
            session.set_environment(environment)
            session.managed_workspace_created = settings.execution_mode == "sandbox"
            if self.storage.folder(session.session_id).exists():
                raise ValueError("Conversation ID already exists; start a fresh new-conversation draft")
            self.switching = True
            self.controls()
            try:
                self.storage.create(session.session_id, workspace=settings.execution_mode == "sandbox")
                if not await self.handoff_sandbox(session, new=True):
                    return False
                await self.switch_session(session, handed_off=True)
                committed = True
            finally:
                self.switching = False
                self.controls()
            self.refresh_history()
            await self.render_session()
            self.flush_render()
            self.controls()
            self.query_one("#composer", TextArea).focus()
            return True
        except (OSError, HistoryError, ValueError, SandboxError, TimeoutError) as exc:
            self.note(str(exc), True)
            return False
        finally:
            if session is not None and not committed and not self.history.path_for(session.session_id).exists():
                self.storage.discard_empty(session.session_id)

    async def action_delete_history(self) -> None:
        if self.busy():
            return
        path = self.history_paths.get(self.session.session_id) if self.session else None
        if path is None:
            self.note("Select a saved chat to delete", True)
            return
        await self.delete_histories([path])

    async def delete_histories(self, paths: list[Path]) -> tuple[list[Path], list[str]]:
        if not paths:
            return [], []
        descriptions = []
        for path in paths:
            try:
                saved = self.history.load(path)
                descriptions.append(f"{saved.title!r} | {saved.model}\n  {path.parent}")
            except HistoryError:
                descriptions.append(str(path.parent))
        if not await self.approve(
            f"Delete {len(paths)} chat(s) permanently?",
            "\n".join(descriptions) + "\n\n"
            "This permanently removes each ENTIRE managed conversation folder, including generated "
            "code and all managed workspace files. It cannot be undone.\n"
            "External host project directories will NOT be deleted. Disconnect running sandboxes first.",
            confirm_label="DELETE PERMANENTLY", destructive=True,
        ):
            return [], []
        deleted: list[Path] = []
        errors: list[str] = []
        active = False
        for path in paths:
            try:
                identifier = self.history.identifier(path)
                await self.check_workspace_unused(identifier, path)
                self.history.delete(path)
            except (HistoryError, ValueError, SandboxError, OSError, TimeoutError) as exc:
                errors.append(str(exc))
            else:
                deleted.append(path)
                active = active or (self.session is not None and self.session.session_id == identifier)
        if active:
            self.host.revoke()
            self.session = None
            self.update_execution_status()
            for identifier in ("chat", "thinking", "activity"):
                self.query_one(f"#{identifier}", RichLog).clear()
            self.query_one("#composer", TextArea).clear()
        self.refresh_history()
        self.controls()
        self.note(
            f"Deleted {len(deleted)} chat folder(s) and their managed files. External host projects unchanged."
            + ("\n" + "\n".join(errors) if errors else ""), bool(errors),
        )
        return deleted, errors

    async def check_workspace_unused(self, identifier: str, path: Path) -> None:
        workspace = self.storage.workspace(identifier)
        if self.editor.active and self.editor.context is not None and (
            self.editor.context.session_id == identifier
            or Path(self.editor.context.workspace) == workspace
        ):
            raise HistoryError("Close the workspace editor before deleting this conversation")
        if self.sandbox.container_id and self.sandbox.workspace == workspace:
            raise HistoryError("Disconnect the attached sandbox before deleting its workspace")
        if not workspace.exists():
            return
        try:
            saved = self.history.load(path)
        except HistoryError as exc:
            raise HistoryError("Cannot prove a damaged conversation workspace is unused; repair its record first") from exc
        if saved.container_id:
            assert saved.environment is not None
            probe = Sandbox(saved.environment.settings(self.launch_settings))
            raw = await probe._cli("ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}")
            matching = any(
                fields and (fields[0].startswith(saved.container_id) or saved.container_id in fields[1:])
                for line in raw.splitlines() if (fields := line.split())
            )
            if matching:
                info = json.loads(await probe._cli("inspect", saved.container_id))[0]
                if info.get("State", {}).get("Running"):
                    raise HistoryError("A recorded container is still running; disconnect/remove or stop it before deleting")

    async def offer_legacy_cleanup(self, *, retry: bool = False) -> None:
        # Test/embedding roots never inspect the real user's previous history.
        if self.storage.root != Path.home() / ".synai":
            return
        receipt = self.storage.root / "legacy-cleanup.json"
        if receipt.exists() and not retry:
            try:
                from synai.storage import checked_path
                checked_path(receipt)
                value = json.loads(receipt.read_text())
                if value.get("status") in {"dismissed", "complete"}:
                    return
                self.legacy_retry = True
                self.legacy_report = "Previous legacy cleanup failed; use RETRY LEGACY CLEANUP."
                return
            except (OSError, ValueError, AttributeError) as exc:
                self.legacy_report = f"Cannot read cleanup receipt: {exc}"
                self.legacy_retry = True
                return
        roots = [Path.home() / ".local/share/local-coding-agent/history"]
        override = os.getenv("AGENT_HISTORY_DIR")
        if override:
            roots.append(Path(override).expanduser().absolute())
            self.legacy_report = "AGENT_HISTORY_DIR is retired; active storage is ~/.synai."
        candidates: list[tuple[History, Path]] = []
        try:
            from synai.storage import checked_path, source_overlap
            for root in dict.fromkeys(roots):
                checked_path(root)
                if root in {Path("/"), Path.home()} or source_overlap(root):
                    raise ValueError("Legacy cleanup directory is too broad or overlaps SynAI source")
                if root == self.storage.root or root.is_relative_to(self.storage.root):
                    raise ValueError("Legacy cleanup cannot target the new storage tree")
                legacy = History(root, self.launch_settings)
                candidates.extend(
                    (legacy, path) for path in legacy.list_paths()
                    if not path.is_symlink() and re.fullmatch(r"[a-f0-9]{32}", path.stem)
                )
            if not candidates:
                return
            if not await self.approve(
                "RESET EXISTING CONVERSATIONS",
                "Delete only these old saved history files? They will not be imported.\n"
                "Existing source/project/workspace files remain untouched. Containers require separate consent.\n\n"
                + "\n".join(str(path) for _, path in candidates),
                confirm_label="DELETE OLD HISTORIES", destructive=True,
            ):
                self.storage.write_receipt({"status": "dismissed"})
                self.legacy_report = "Legacy cleanup declined; old histories and containers were preserved."
                return
            containers: list[tuple[Sandbox, str, Path]] = []
            retained: list[str] = []
            for legacy, path in candidates:
                try:
                    session = legacy.load(path)
                except HistoryError:
                    continue
                if session.container_id:
                    settings = legacy.environment_for(session).settings(self.launch_settings)
                    probe = Sandbox(settings)
                    try:
                        info = json.loads(await probe._cli("inspect", session.container_id))[0]
                        owner = info.get("Config", {}).get("Labels", {}).get("local-coding-agent.owner")
                        if owner != self.sandbox.owner:
                            retained.append(f"{settings.runtime}: {session.container_id} (ownership unverifiable)")
                            continue
                        containers.append((probe, session.container_id, path))
                    except (ValueError, KeyError, IndexError, SandboxError, OSError, TimeoutError) as exc:
                        retained.append(f"{settings.runtime}: {session.container_id} (retained: {exc})")
            errors: list[str] = []
            blocked: set[Path] = set()
            if containers:
                remove = await self.approve(
                    "REMOVE VERIFIED APP-OWNED LEGACY CONTAINERS",
                    "Separately remove these verified app-owned containers? Old workspace files remain.\n"
                    + "\n".join(f"{probe.settings.runtime}: {identifier}" for probe, identifier, _ in containers),
                    confirm_label="REMOVE OWNED CONTAINERS", destructive=True,
                )
                for probe, identifier, path in containers:
                    if not remove:
                        retained.append(f"{probe.settings.runtime}: {identifier} (removal declined)")
                        continue
                    probe.owner, probe.owned, probe.container_id = self.sandbox.owner, True, identifier
                    try:
                        await probe.remove_owned()
                    except (ValueError, SandboxError, OSError, TimeoutError) as exc:
                        blocked.add(path)
                        errors.append(f"{path}: container removal failed: {exc}")
            if retained and not await self.approve(
                "CONTAINERS WILL REMAIN",
                "\n".join(retained) + "\n\nThese containers will NOT be removed. "
                "Inspect/stop these specific IDs manually using their runtime. "
                "Proceed with history-only deletion anyway?",
                confirm_label="DELETE HISTORIES ONLY", destructive=True,
            ):
                self.storage.write_receipt({"status": "dismissed", "retained_containers": retained})
                self.legacy_report = "Legacy history deletion cancelled; retained containers unchanged."
                return
            deleted = 0
            for legacy, path in candidates:
                if path in blocked:
                    continue
                try:
                    legacy.delete(path)
                    deleted += 1
                except HistoryError as exc:
                    errors.append(str(exc))
            self.legacy_retry = bool(errors)
            self.legacy_report = (
                f"Deleted {deleted} old histories; old workspace files preserved."
                + ("\nRetained containers:\n" + "\n".join(retained) if retained else "")
                + ("\n" + "\n".join(errors) if errors else "")
            )
            self.storage.write_receipt({
                "status": "failed" if errors else "complete", "deleted": deleted,
                "errors": errors, "retained_containers": retained,
            })
        except (ValueError, HistoryError, OSError, AttributeError) as exc:
            self.legacy_retry = True
            self.legacy_report = f"Legacy cleanup failed: {exc}"
        finally:
            if self.legacy_report:
                self.notify(self.legacy_report, severity="warning" if self.legacy_retry else "information", timeout=15)

    def action_manage_history(self) -> None:
        if self.busy():
            return
        self.open_history_manager()

    def open_history_manager(self, menu: MainMenu | None = None) -> None:
        if self.history_manager_open:
            return
        self.history_manager_open = True

        def closed(value: None) -> None:
            self.history_manager_open = False
            self.controls()

        self.push_screen(HistoryScreen(self, menu), closed)
        self.controls()

    async def approve(
        self, title: str, details: str, confirm_label: str = "ALLOW ONCE", destructive: bool = False,
    ) -> bool:
        if self.approval_future is not None:
            raise RuntimeError("An approval is already pending")
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approval_future = future

        def resolved(value: bool | None) -> None:
            if not future.done():
                future.set_result(value is True)

        modal = ApprovalScreen(title, details, confirm_label, destructive)
        try:
            if self.session:
                self.session.activity.append(Activity("approval", f"Requested: {title}"))
                self.history.save(self.session)
            self.push_screen(modal, resolved)
            self.controls()
            allowed = await future
            if self.session:
                self.session.activity.append(Activity("approval", f"{'Allowed' if allowed else 'Denied'}: {title}"))
                self.history.save(self.session)
            return allowed
        finally:
            self.approval_future = None
            if self.screen is modal:
                self.pop_screen()
            self.controls()

    async def setup_sandbox(self, action: str, container: str = "") -> bool:
        try:
            self.guard_editor_context()
            if self.session is None and action != "disconnect":
                raise SandboxError("Create or open a conversation before configuring its sandbox.")
            workspace = Path(self.session.workspace) if self.session else Path.cwd()
            if self.settings.execution_mode == "host":
                if action != "disconnect":
                    raise SandboxError("Select and save sandbox mode before creating/attaching a container")
                self.host.revoke()
                self.update_execution_status()
                self.note("Host tools disconnected; saved mode and workspace unchanged")
                return True
            if action == "disconnect":
                if await self.approve("Disconnect sandbox", "Remove the currently app-owned container, or detach an existing one?\nWorkspace files will not be deleted."):
                    await self.sandbox.remove_owned()
                else:
                    return False
            elif action == "create":
                image = self.settings.image
                if not await self.approve(
                    "Create sandbox",
                    f"Pull image {image!r} and create a non-root container?\n"
                    f"Writable workspace: {workspace}\nNetwork enabled. Only trust images you control.\n"
                    "The selected workspace and anything in it will be accessible to approved commands.",
                ):
                    return False
                self.note("Pulling image and creating restricted container...")
                await self.sandbox.pull(image)
                await self.sandbox.create(workspace, image)
            else:
                if await self.approve("Attach sandbox", f"Validate and attach container {container!r} to workspace {workspace}?"):
                    await self.sandbox.attach(container, workspace)
                else:
                    return False
            self.update_execution_status()
            if self.sandbox.workspace:
                if self.session and Path(self.session.workspace) == self.sandbox.workspace:
                    self.session.container_id = self.sandbox.container_id
                    self.history.save(self.session)
                else:
                    self.session = None
            self.note("Sandbox configuration updated")
            return True
        except (SandboxError, OSError, TimeoutError, HistoryError) as exc:
            self.note(str(exc), True)
            raise

    def execution_status(self) -> str:
        host_mode = self.settings.execution_mode == "host"
        ready = self.session is not None and self.host.matches(self.session)
        return (
            "HOST // NOT ISOLATED\n" + ("Tools approved for this chat" if ready else "Chat-only: host tools not authorized")
            + (f"\n{self.host_error}" if self.host_error else "")
            if host_mode else
            f"Non-root sandbox ready:\n{self.sandbox.container_id[:12]}"
            if self.sandbox.healthy and self.sandbox.container_id else "Chat-only: no sandbox attached"
        )

    def update_execution_status(self) -> None:
        self.update_summary()

    async def enable_host_tools(self) -> bool:
        self.host.revoke()
        self.host_error = None
        self.update_execution_status()
        if self.session is None or self.settings.execution_mode != "host":
            raise ValueError("Select and save host mode before enabling host tools")
        try:
            if not await self.approve(
                "ENABLE HOST TOOLS // NO SANDBOX",
                f"Run model tools as Linux user {os.getuid()} on this host?\n"
                f"Starting workspace: {self.session.workspace}\n\n"
                "NOT ISOLATED: approved commands/programs can read, change or transmit anything "
                "your account can access, including outside this workspace. Container memory/CPU/PID "
                "limits do not apply. Network access and existing group privileges remain available.\n"
                "File tools stay workspace-relative; terminal, edits and network actions still need approval. "
                "SynAI refuses root, enables no-new-privileges and rejects sudo requests, but these are "
                "not a sandbox. Use a dedicated account without sudo or container-daemon privileges.\n"
                "Detached processes may outlive Stop. Deny keeps this conversation chat-only.",
                confirm_label="ENABLE HOST TOOLS",
            ):
                return False
            self.host.activate(self.session)
            return True
        except (ValueError, SandboxError, OSError) as exc:
            self.host_error = f"Host tools unavailable: {exc}"
            self.note(self.host_error, True)
            return False
        finally:
            self.update_execution_status()

    async def action_send(self) -> None:
        if self.busy() or self.session is None:
            return
        prompt = self.query_one("#composer", TextArea).text
        if not prompt.strip():
            self.note("Enter a prompt first", True)
            return
        if self.agent.provider is not self.provider or self.agent.connection_endpoint != self.settings.ollama_url:
            self.note("Connection is changing; wait before sending.", True)
            return
        if not Path(self.session.workspace).is_dir():
            self.note("Saved workspace is missing. Edit the conversation environment before sending.", True)
            return
        backend = self.agent.tools.sandbox
        if backend.workspace is not None and str(backend.workspace) != self.session.workspace:
            self.note("Execution workspace does not match this conversation. Disconnect tools before sending.", True)
            return
        model = self.models.get(self.session.model)
        if model is None:
            self.note("This conversation's model is unavailable. Choose a replacement model or change Connection Settings in the main menu.", True)
            return
        if self.settings.execution_mode == "sandbox" and self.sandbox.healthy:
            try:
                await self.sandbox.validate()
            except (SandboxError, OSError, TimeoutError) as exc:
                self.sandbox.healthy = False
                self.note(f"Sandbox unavailable, continuing in chat-only mode: {exc}", True)
        self.query_one("#composer", TextArea).clear()
        self.session.limits = {
            "command_timeout": self.settings.command_timeout,
            "output_bytes": self.settings.output_bytes, "tool_budget": self.settings.tool_budget,
        }
        self.turn_task = asyncio.create_task(self.run_turn(prompt, model))
        self.controls()

    async def run_turn(self, prompt: str, model: ModelInfo) -> None:
        assert self.session is not None
        try:
            await self.agent.turn(self.session, model, prompt)
        except asyncio.CancelledError:
            self.note("Stopped. Partial response retained.")
        except (HistoryError, SandboxError, OSError, TimeoutError, ValueError) as exc:
            if self.session:
                self.session.state = "error"
                self.session.activity.append(Activity("error", str(exc)))
                self.render_dirty = True
            self.note(str(exc), True)
        finally:
            self.turn_task = None
            self.controls()
            self.refresh_history()
            self.flush_render()

    async def action_stop(self) -> None:
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
            await self.turn_task

    async def render_session(self) -> None:
        self.render_dirty = True

    def flush_render(self) -> None:
        if self.shutting_down or not self.is_running or not self.render_dirty or self.session is None:
            return
        self.render_dirty = False
        self.transient_notes.clear()
        for identifier in ("chat", "thinking", "activity"):
            self.query_one(f"#{identifier}", RichLog).clear()
        for message in self.session.messages:
            if message.role in {"user", "assistant"}:
                color = "synai.primary" if message.role == "user" else "synai.secondary"
                speaker = (message.source.model if message.source else self.session.model) if message.role == "assistant" else "USER"
                self.query_one("#chat", RichLog).write(Text(f"{speaker} [{message.status}]", style=color))
                content = message.content
                self.query_one("#chat", RichLog).write(Text(
                    ("[Earlier text hidden in UI; full text in history]\n" if len(content) > 64000 else "") + content[-64000:],
                    style="synai.text",
                ))
                if message.thinking:
                    self.query_one("#thinking", RichLog).write(Text(message.thinking[-64000:], style="synai.accent"))
        if not any(message.thinking for message in self.session.messages):
            self.query_one("#thinking", RichLog).write(Text("No provider-emitted reasoning received.", style="synai.text"))
        for activity in self.session.activity[-100:]:
            self.query_one("#activity", RichLog).write(format_activity(activity, themed=True))
        self.query_one("#status", Static).update(Text(f"{self.session.model} // {self.session.state} // {self.session.title}"))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id
        if identifier == "send":
            await self.action_send()
        elif identifier == "stop":
            await self.action_stop()

    async def disconnect_sandbox(self) -> None:
        try:
            await self.setup_sandbox("disconnect")
        except (SandboxError, OSError, TimeoutError, HistoryError):
            pass  # setup_sandbox already reports the failure to the UI.
        finally:
            self.setup_task = None
            self.controls()

    async def action_quit_agent(self) -> None:
        if isinstance(self.screen, ModalScreen) and not isinstance(self.screen, MainMenu):
            return
        if self.setup_task and not self.setup_task.done():
            self.note("Wait for sandbox setup to finish before quitting", True)
            return
        try:
            if not await self.editor.close():
                self.note("Quit cancelled; workspace editor remains open")
                return
        except (EditorError, OSError, TimeoutError) as exc:
            self.note(f"Cannot quit while the editor is open: {exc}", True)
            return
        await self.action_stop()
        self.host.revoke()
        if self.sandbox.owned:
            if await self.approve("Remove sandbox on exit?", "Remove only this app's container? Workspace files remain.\nDeny leaves it running; reconnect using its container ID later."):
                try:
                    await self.sandbox.remove_owned()
                except (SandboxError, OSError, TimeoutError) as exc:
                    self.note(str(exc), True)
                    return
        await self.provider.close()
        self.exit()
