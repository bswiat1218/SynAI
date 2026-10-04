from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Never
from uuid import uuid4

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, ContentSwitcher, Input, Label, Select, Static, TextArea

from synai.config import ConversationEnvironment
from synai.history import HistoryError
from synai.models import ModelInfo
from synai.providers.base import ProviderError
from synai.providers.ollama import OllamaProvider
from synai.sandbox import SandboxError
from synai.tui.directory_picker import DirectoryPicker
from synai.tui.navigation import Direction, MenuBody, MenuInput, MenuScreen, MenuSelect

if TYPE_CHECKING:
    from synai.tui.application import CodingApp


class MainMenu(MenuScreen[None]):
    BINDINGS = [
        ("escape", "back", "Return to chat"),
        ("f2", "back", "Return to chat"),
        ("alt+n", "activate('menu-new')", "New"),
        ("alt+c", "activate('menu-histories')", "Conversations"),
        ("alt+s", "activate('menu-environment')", "Settings"),
        ("alt+m", "model", "Model"),
        ("alt+o", "activate('menu-connection')", "Connection"),
        ("alt+r", "activate('menu-refresh')", "Refresh"),
        ("alt+d", "activate('disconnect')", "Disconnect"),
        ("alt+h", "help", "Help"),
        ("alt+x", "activate('menu-stop')", "Stop"),
    ]

    def __init__(self, app: CodingApp) -> None:
        super().__init__()
        self.coding_app = app
        self.displayed_text: dict[str, str] = {}
        self.model_snapshot: tuple[tuple[str, ...], str | None, bool, bool] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dashboard"):
            yield Label("▓▒░ SynAI // F2 DASHBOARD ░▒▓", classes="modal-title")
            yield Static("", id="menu-status")
            with MenuBody(id="dashboard-content"):
                with Horizontal(id="dashboard-columns"):
                    with Vertical(classes="dashboard-column"):
                        with Vertical(classes="dashboard-group"):
                            yield Label("CONVERSATIONS", classes="menu-section")
                            yield Button("NEW CONVERSATION", id="menu-new", variant="success")
                            yield Button("CONVERSATIONS", id="menu-histories")
                            yield Button("RETURN TO CHAT", id="menu-back")
                        with Vertical(id="menu-current-group", classes="dashboard-group"):
                            yield Label("CURRENT CONVERSATION", classes="menu-section")
                            yield MenuSelect([], prompt="Change model", id="model")
                            yield Static("", id="capability")
                            yield Button("SETTINGS", id="menu-environment")
                            yield Button("CONVERSATION DETAILS", id="menu-details-open")
                            yield Button("WORKSPACE EDITOR", id="menu-editor")
                            yield Button("DISCONNECT / REMOVE", id="disconnect")
                            yield Button("STOP RESPONSE", id="menu-stop", variant="error")
                    with Vertical(classes="dashboard-column"):
                        with Vertical(classes="dashboard-group"):
                            yield Label("CONNECTION", classes="menu-section")
                            yield Static("", id="menu-connection-status")
                            yield Button("CONNECTION SETTINGS", id="menu-connection")
                            yield Button("REFRESH MODELS", id="menu-refresh")
                        with Vertical(classes="dashboard-group"):
                            yield Label("APPLICATION", classes="menu-section")
                            yield Button("THEMES", id="menu-theme")
                            yield Button("RETRY LEGACY CLEANUP", id="menu-legacy")
                            yield Button("QUIT SynAI", id="menu-quit", variant="error")
            yield Static("Arrows: navigate | Enter: open / edit | Esc: back | F1: Help", id="dashboard-hints")

    def on_mount(self) -> None:
        self.set_interval(0.2, self.update_status)
        self.update_status()
        self.query_one("#menu-back" if self.coding_app.session else "#menu-new", Button).focus()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 70, "narrow")

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            self.update_status()

    def update_text(self, identifier: str, text: str) -> None:
        if self.displayed_text.get(identifier) != text:
            self.query_one(f"#{identifier}", Static).update(Text(text))
            self.displayed_text[identifier] = text

    def update_status(self) -> None:
        app = self.coding_app
        idle = not app.operation_busy()
        session = app.session
        connection = "Discovering models..." if app.loading else (
            app.connection_error or f"{len(app.models)} model(s) available"
        )
        status = (
            f"{'Ready' if idle else 'Operation active // changes locked'} // "
            f"{session.title if session else 'No conversation selected'}"
            + (f"\n{app.legacy_report}" if app.legacy_report else "")
            + (f"\n{app.storage_error}" if app.storage_error else "")
        )
        self.update_text("menu-status", status)
        self.update_text("menu-connection-status", f"{app.settings.ollama_url}\n{connection}")
        for identifier in ("menu-new", "menu-environment", "menu-histories", "disconnect",
                           "menu-refresh", "menu-connection", "menu-legacy"):
            self.query_one(f"#{identifier}", Button).disabled = not idle or (
                app.storage_error is not None and identifier in {"menu-new", "menu-environment", "menu-histories"}
            ) or (app.loading and identifier == "menu-refresh")
        self.query_one("#menu-legacy", Button).display = app.legacy_retry
        self.query_one("#menu-back", Button).disabled = session is None
        self.query_one("#menu-back", Button).display = session is not None
        for identifier in ("menu-current-group",):
            self.query_one(f"#{identifier}").display = session is not None
        select = self.query_one("#model", Select)
        select.disabled = not idle or session is None or app.loading
        snapshot = (tuple(app.models), session.model if session else None, app.loading, app.switching)
        if snapshot != self.model_snapshot:
            with select.prevent(Select.Changed):
                select.set_options([(name, name) for name in app.models])
                select.value = session.model if session and session.model in app.models else Select.NULL
            self.model_snapshot = snapshot
        self.update_text("capability", f"{app.capability_status()}\n{app.execution_status()}")
        running = bool(app.turn_task and not app.turn_task.done())
        self.query_one("#menu-stop", Button).disabled = not running
        self.query_one("#menu-stop", Button).display = running
        self.query_one("#disconnect", Button).label = (
            "DISCONNECT HOST TOOLS" if app.settings.execution_mode == "host"
            else "DISCONNECT / REMOVE"
        )
        self.query_one("#menu-details-open", Button).disabled = session is None
        self.query_one("#menu-editor", Button).disabled = session is None or not idle
        if app.editor.active:
            for identifier in ("menu-new", "menu-environment", "disconnect"):
                self.query_one(f"#{identifier}", Button).disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.disabled:
            return
        self.activate(event.button.id)

    def action_activate(self, identifier: str) -> None:
        button = self.query_one(f"#{identifier}", Button)
        self.update_status()
        if not button.disabled and button.display:
            self.activate(identifier)

    def action_model(self) -> None:
        self.update_status()
        selector = self.query_one("#model", Select)
        if not selector.disabled:
            selector.focus()
            selector.scroll_visible(animate=False)

    def action_help(self) -> None:
        self.coding_app.action_help()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        app = self.coding_app
        if event.select.id == "model" and event.value is not Select.NULL and app.dashboard_idle():
            name = str(event.value)
            if app.session is not None and name != app.session.model:
                self.run_worker(self.change_model(name), exclusive=True, group="model-change")

    async def change_model(self, name: str) -> None:
        await self.coding_app.change_model(name)
        self.model_snapshot = None
        self.update_status()
        if self.coding_app.screen is self:
            self.query_one("#model", Select).focus()

    def activate(self, action: str | None) -> None:
        app = self.coding_app
        if action not in {"menu-back", "menu-stop", "menu-theme", "menu-quit", "menu-details-open"} and not app.dashboard_idle():
            return
        if action == "menu-new":
            app.push_screen(EnvironmentMenu(app, new=True))
        elif action == "menu-environment":
            if app.session is not None:
                app.push_screen(EnvironmentMenu(app))
        elif action == "menu-details-open" and app.session is not None:
            app.push_screen(ConversationDetails(app))
        elif action == "menu-editor":
            app.action_workspace_editor()
        elif action == "menu-histories":
            app.open_history_manager(self)
        elif action == "menu-legacy":
            app.run_worker(app.offer_legacy_cleanup(retry=True), exclusive=True, group="legacy-cleanup")
        elif action == "menu-refresh":
            app.run_worker(app.refresh_models(), exclusive=True, group="models")
        elif action == "menu-connection":
            app.push_screen(ConnectionMenu(app))
        elif action == "menu-theme":
            app.action_themes()
        elif action == "menu-stop":
            app.run_worker(app.action_stop(), exclusive=True, group="stop")
        elif action == "disconnect" and app.session is not None:
            app.setup_task = asyncio.create_task(app.disconnect_sandbox())
            app.controls()
        elif action == "menu-back":
            self.action_back()
        elif action == "menu-quit":
            app.run_worker(app.action_quit_agent(), exclusive=True, group="quit")

    def action_back(self) -> None:
        if self.coding_app.session is not None:
            self.dismiss(None)
            self.coding_app.controls()


class ConversationMenu(MenuScreen[None]):
    BINDINGS = [("escape", "back", "Back")]

    def __init__(
        self, app: CodingApp, menu: EnvironmentMenu, *,
        environment: ConversationEnvironment | None = None,
        preferred_model: str | None = None,
    ) -> None:
        super().__init__()
        self.coding_app, self.menu = app, menu
        self.opening = False
        self.environment = environment
        self.preferred_model = preferred_model
        self.draft_provider: OllamaProvider | None = None
        self.draft_models: set[str] = set()
        self.connection_epoch = app.environment_epoch
        self.model_info: dict[str, ModelInfo] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-shell"):
            yield Label("CHOOSE MODEL", classes="modal-title")
            with MenuBody(classes="modal-body"):
                yield Static("Choose a model for this conversation.")
                yield MenuSelect([], id="conversation-choice", disabled=True)
                yield Static("", id="conversation-message")
            with Horizontal(classes="modal-actions"):
                yield Button("CREATE CONVERSATION", id="conversation-open", variant="success")
                yield Button("BACK", id="conversation-back")
            yield Static("Enter: open model / create | Esc: back | F1: Help", classes="menu-hints")

    def on_mount(self) -> None:
        app = self.coding_app
        assert self.environment is not None
        self.query_one("#conversation-open", Button).disabled = True
        self.run_worker(self.discover_models(), exclusive=True, group="draft-models")
        self.query_one("#conversation-message", Static).update(Text(
            f"Workspace: {self.environment.workspace}\nGlobal Ollama: {app.settings.ollama_url}\n"
            "Discovering models on the application connection...\n"
            + ("Host tools require explicit host-access confirmation."
             if self.environment.execution_mode == "host" else
             "Tools require an approved sandbox; creating a chat does not start one.")
        ))

    async def discover_models(self) -> None:
        assert self.environment is not None
        self.draft_provider = OllamaProvider(self.coding_app.settings.ollama_url, self.coding_app.settings.request_timeout)
        try:
            models = await self.draft_provider.list_models()
            if self.connection_epoch != self.coding_app.environment_epoch:
                raise ProviderError("Connection changed; return to the main menu and retry.")
            self.draft_models = {model.name for model in models}
            self.model_info = {model.name: model for model in models}
            choices = self.query_one("#conversation-choice", Select)
            choices.disabled = not bool(models)
            choices.set_options([(model.name, model.name) for model in models])
            if models:
                choices.value = self.preferred_model if self.preferred_model in self.draft_models else models[0].name
                self.query_one("#conversation-open", Button).disabled = False
                message = "Choose a model. The reviewed environment will be saved with this conversation."
            else:
                message = "No models at this endpoint. Return to the main menu to edit Connection Settings."
            self.query_one("#conversation-message", Static).update(Text(message))
            if self.app.screen is self and self.focused is None:
                choices.focus()
        except ProviderError as exc:
            self.query_one("#conversation-message", Static).update(Text(f"{exc}\nReturn to the main menu to edit Connection Settings.", style="synai.error"))
        finally:
            await self.draft_provider.close()
            self.draft_provider = None

    async def on_unmount(self) -> None:
        if self.draft_provider is not None:
            await self.draft_provider.close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "conversation-back":
            self.action_back()
        elif event.button.id == "conversation-open" and not self.opening:
            self.opening = True
            self.query_one("#conversation-open", Button).disabled = True
            self.run_worker(self.open_conversation(), exclusive=True, group="open-conversation")

    async def open_conversation(self) -> None:
        try:
            value = self.query_one("#conversation-choice", Select).value
            if value is Select.NULL:
                raise ValueError("Choose a model first.")
            app = self.coding_app
            name = str(value)
            if name not in self.draft_models or self.connection_epoch != app.environment_epoch:
                raise ValueError("Choose an available model.")
            app.models = dict(self.model_info)
            if not await app.create_session(name, self.environment, self.menu.conversation_id):
                raise ValueError("Could not create conversation; check workspace and storage permissions.")
            app.query_one("#composer", TextArea).clear()
            self.dismiss(None)
            self.menu.dismiss(None)
            if isinstance(app.screen, MainMenu):
                app.screen.dismiss(None)
            app.controls()
            app.query_one("#composer").focus()
        except (HistoryError, OSError, ValueError, KeyError, SandboxError, TimeoutError) as exc:
            self.query_one("#conversation-message", Static).update(Text(str(exc), style="synai.error"))
        finally:
            self.opening = False
            if self.is_mounted:
                self.query_one("#conversation-open", Button).disabled = False

    def action_back(self) -> None:
        if not self.opening:
            self.dismiss(None)


class ConnectionMenu(MenuScreen[None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    INITIAL_FOCUS = "#connection-url"

    def __init__(self, app: CodingApp) -> None:
        super().__init__()
        self.coding_app = app
        self.saving = False

    def compose(self) -> ComposeResult:
        app = self.coding_app
        with Vertical(classes="modal-shell"):
            yield Label("APPLICATION CONNECTION", classes="modal-title")
            with MenuBody(classes="modal-body"):
                yield Static(
                    "One connection for every conversation. Use a trusted Ollama server.\n"
                    "Endpoint changes can expose retained conversation context to the new server.\n"
                    f"Launch sources: {app.connection_sources or 'supplied application settings'}\n"
                    f"Saved: {app.preferences.ollama_url} / {app.preferences.request_timeout:g}s\n"
                    "Explicit CLI/environment overrides win again on the next launch."
                )
                yield Label("Ollama endpoint")
                yield MenuInput(app.settings.ollama_url, id="connection-url")
                yield Label("Provider idle timeout (seconds)")
                yield MenuInput(str(app.settings.request_timeout), id="connection-timeout")
            yield Static("", id="connection-result")
            with Horizontal(classes="modal-actions"):
                yield Button("SAVE CONNECTION", id="connection-save", variant="success")
                yield Button("CANCEL", id="connection-cancel")
            yield Static("Enter: edit / finish | Esc: finish edit / cancel | F1: Help", classes="menu-hints")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "connection-cancel":
            self.action_cancel()
        elif event.button.id == "connection-save" and not self.saving:
            self.saving = True
            for widget in self.query("Button, Input"):
                widget.disabled = True
            self.run_worker(self.save(), exclusive=True, group="connection-save")

    async def save(self) -> None:
        try:
            url = self.query_one("#connection-url", Input).value
            timeout = float(self.query_one("#connection-timeout", Input).value)
            if await self.coding_app.apply_connection(url, timeout):
                self.query_one("#connection-result", Static).update(Text(
                    "Connection saved. " + (self.coding_app.connection_error or "Models refreshed."),
                    style="synai.warning" if self.coding_app.connection_error else "synai.primary",
                ))
            else:
                self.query_one("#connection-result", Static).update("Cancelled; connection unchanged.")
        except (ValueError, OSError, TimeoutError, ProviderError) as exc:
            self.query_one("#connection-result", Static).update(Text(str(exc), style="synai.error"))
        finally:
            self.saving = False
            if self.is_mounted:
                for widget in self.query("Button, Input"):
                    widget.disabled = False

    def action_cancel(self) -> None:
        if not self.saving:
            self.dismiss(None)


class EnvironmentMenu(MenuScreen[None]):
    BINDINGS = [("escape", "back", "Back")]
    INITIAL_FOCUS = "#config-nav-overview"
    FIELDS = (
        ("workspace", "Workspace directory"),
        ("image", "Trusted container image"),
        ("command_timeout", "Command timeout (seconds)"),
        ("output_bytes", "Output limit (bytes, minimum 1024)"),
        ("tool_budget", "Tool calls per turn"),
        ("memory", "Container memory (e.g. 1g)"),
        ("cpus", "Container CPU limit"),
        ("pids", "Container PID limit"),
    )
    PAGES = {
        "overview": ("Overview", ()),
        "sandbox": ("Execution", ("image",)),
        "workspace": ("Workspace", ("workspace",)),
        "limits": ("Limits", ("command_timeout", "output_bytes", "tool_budget", "memory", "cpus", "pids")),
    }

    def __init__(self, app: CodingApp, *, new: bool = False, preferred_model: str | None = None) -> None:
        super().__init__()
        self.coding_app = app
        self.applying = False
        self.new = new
        self.preferred_model = preferred_model
        self.initial_settings = app.launch_settings if new else app.settings
        self.conversation_id = uuid4().hex if new or app.session is None else app.session.session_id
        self.host_workspace = str(app.launch_workspace) if new else (
            app.session.workspace if app.session else str(app.launch_workspace)
        )
        if not new and self.initial_settings.execution_mode == "sandbox":
            self.host_workspace = str(app.launch_workspace)
        self.managed_workspace = str(app.storage.workspace(self.conversation_id))
        self.initial_workspace = (
            self.managed_workspace if self.initial_settings.execution_mode == "sandbox" else self.host_workspace
        )
        self.sandbox_working = False
        self.baseline: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="environment-card modal-shell"):
            yield Label("NEW CONVERSATION SETUP" if self.new else "CONVERSATION SETTINGS", classes="modal-title")
            yield Static("", id="configuration-draft-status")
            with Horizontal(id="configuration-body"):
                with MenuBody(id="configuration-nav"):
                    for page, (title, fields) in self.PAGES.items():
                        yield Button(title, id=f"config-nav-{page}", classes="config-nav")
                with ContentSwitcher(initial="config-page-overview", id="configuration-pages"):
                    for page, (title, fields) in self.PAGES.items():
                        with MenuBody(id=f"config-page-{page}", classes="configuration-page"):
                            yield Label(title.upper(), classes="configuration-title")
                            if page == "overview":
                                yield Static("", id="configuration-overview")
                                yield Static("Navigate pages without saving. Save/Next validates all pages.\n"
                                             "Configuration belongs only to this conversation.\n"
                                             "No container is created or attached automatically.")
                            if page == "sandbox":
                                yield Label("Execution environment")
                                yield MenuSelect(
                                    [("Sandbox (Docker/Podman)", "sandbox"), ("Host (no sandbox)", "host")],
                                    value=self.initial_settings.execution_mode, allow_blank=False,
                                    id="env-execution_mode",
                                )
                                yield Static(
                                    "HOST IS NOT ISOLATED. Approved commands can access anything your account can access. "
                                    "Container resource limits do not apply. Fresh host-access confirmation is required.",
                                    id="config-host-warning",
                                )
                            for key in fields:
                                label = dict(self.FIELDS)[key]
                                yield Label(label, id=f"env-{key}-label")
                                value = self.initial_workspace if key == "workspace" else str(getattr(self.initial_settings, key))
                                yield MenuInput(value, id=f"env-{key}")
                            if page == "workspace":
                                yield Button("CHOOSE DIRECTORY", id="config-workspace-picker")
                                yield Static("", id="config-workspace-help")
                            elif page == "limits":
                                yield Static("", id="config-limit-help")
                            elif page == "sandbox":
                                yield Label("Container runtime", id="env-runtime-label")
                                yield MenuSelect([("Docker", "docker"), ("Podman", "podman")],
                                             value=self.initial_settings.runtime, allow_blank=False, id="env-runtime")
                                yield Static("Save changes on every page before creating/attaching. Actions require approval.")
                                if not self.new:
                                    yield Static("", id="config-sandbox-status")
                                    yield MenuInput(self.coding_app.session.container_id or "" if self.coding_app.session else "",
                                                placeholder="Existing container ID/name", id="config-container")
                                    yield Button("CREATE SANDBOX", id="config-create", variant="warning")
                                    yield Button("ATTACH SANDBOX", id="config-attach")
                                    yield Button("ENABLE HOST TOOLS", id="config-host-enable", variant="warning")
                                else:
                                    yield Static("Create the conversation first. Sandbox actions then live here; "
                                                 "host mode requests confirmation when the conversation opens.")
            yield Static("", id="environment-result")
            with Horizontal(id="configuration-actions", classes="modal-actions"):
                yield Button("NEXT" if self.new else "SAVE SETTINGS", id="environment-apply", variant="success")
                yield Button("CANCEL" if self.new else "CLOSE", id="environment-back")
            yield Static("Arrows: page / field | Enter: edit | Esc: finish / close", classes="menu-hints")

    def on_mount(self) -> None:
        self.baseline = self.form_values()
        self.show_page("overview")
        self.refresh_sandbox_controls()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 105, "narrow")

    def form_values(self) -> dict[str, str]:
        values = {key: self.query_one(f"#env-{key}", Input).value for key, label in self.FIELDS}
        values["runtime"] = str(self.query_one("#env-runtime", Select).value)
        values["execution_mode"] = str(self.query_one("#env-execution_mode", Select).value)
        return values

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        super().on_descendant_focus(event)
        identifier = event.widget.id or ""
        if self.baseline and identifier.startswith("config-nav-"):
            page = identifier.removeprefix("config-nav-")
            if page in self.PAGES:
                self.show_page(page)

    async def action_navigate(self, direction: Direction) -> None:
        focused = self.focused
        current = self.query_one("#configuration-pages", ContentSwitcher).current
        if current is not None and focused is not None:
            page = current.removeprefix("config-page-")
            if direction == "right" and (focused.id or "").startswith("config-nav-"):
                panel = self.query_one(f"#{current}")
                target = next((control for control in self.focus_chain if panel in control.ancestors), None)
                if target is not None:
                    target.focus()
                    target.scroll_visible(animate=False)
                    return
            if direction == "left" and isinstance(focused, (MenuInput, MenuSelect)):
                if not (isinstance(focused, MenuInput) and focused.editing) and not (
                    isinstance(focused, MenuSelect) and focused.expanded
                ):
                    self.query_one(f"#config-nav-{page}", Button).focus()
                    return
        await super().action_navigate(direction)

    def body_for_scroll(self) -> MenuBody | None:
        current = self.query_one("#configuration-pages", ContentSwitcher).current
        return self.query_one(f"#{current}", MenuBody) if current is not None else None

    def update_overview(self) -> None:
        values = self.form_values()
        dirty = values != self.baseline
        status = "Unsaved changes" if dirty else "New draft defaults" if self.new else "Saved conversation configuration"
        attached = self.coding_app.sandbox.container_id is not None and not self.new
        self.query_one("#configuration-draft-status", Static).update(Text(
            status + (" // Disconnect sandbox to edit" if attached else ""),
            style="synai.warning" if dirty or attached else "synai.muted",
        ))
        self.query_one("#configuration-overview", Static).update(Text(
            status
            + "\nForm values (validated on Save/Next):\n"
            f"Storage: {self.coding_app.storage.folder(self.conversation_id)}\n"
            f"Mode: {values['execution_mode']}\n"
            f"Workspace: {values['workspace']}\nGlobal Ollama (main-menu settings): {self.coding_app.settings.ollama_url}\n"
            f"Runtime / image: {values['runtime']} / {values['image']}\n"
            f"Command timeout: {values['command_timeout']}s\n"
            f"Output: {values['output_bytes']} bytes | Tool budget: {values['tool_budget']}\n"
            f"Container resources {'(inactive on host)' if values['execution_mode'] == 'host' else ''}: "
            f"{values['memory']} RAM / {values['cpus']} CPUs / {values['pids']} PIDs\n"
            f"Tools: {'host approved' if self.coding_app.session and self.coding_app.host.matches(self.coding_app.session) else self.coding_app.sandbox.container_id or 'disconnected / unconfirmed'}",
            style="synai.warning" if dirty else "synai.muted",
        ))

    def show_page(self, page: str) -> None:
        self.query_one("#configuration-pages", ContentSwitcher).current = f"config-page-{page}"
        for key in self.PAGES:
            self.query_one(f"#config-nav-{key}", Button).set_class(key == page, "active-page")
        self.update_overview()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.baseline:
            self.update_overview()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if self.baseline:
            if event.select.id == "env-execution_mode":
                field = self.query_one("#env-workspace", Input)
                if event.value == "sandbox":
                    if field.value != self.managed_workspace:
                        self.host_workspace = field.value
                    field.value = self.managed_workspace
                else:
                    field.value = self.host_workspace
            self.refresh_sandbox_controls()

    def field_error(self, key: str, message: str) -> Never:
        page = "sandbox" if key in {"runtime", "execution_mode"} else next(
            page for page, (title, fields) in self.PAGES.items() if key in fields
        )
        self.show_page(page)
        field = self.query_one(f"#env-{key}")
        self.call_after_refresh(field.focus)
        self.call_after_refresh(field.scroll_visible, animate=False)
        if isinstance(field, MenuInput):
            self.call_after_refresh(field.begin_edit)
        raise ValueError(message)

    def refresh_sandbox_controls(self) -> None:
        attached = self.coding_app.sandbox.container_id is not None and not self.new
        host_mode = self.query_one("#env-execution_mode", Select).value == "host"
        locked = attached or self.sandbox_working or self.applying
        for key, label in self.FIELDS:
            self.query_one(f"#env-{key}", Input).disabled = locked or (
                host_mode and key in {"image", "memory", "cpus", "pids"}
            ) or key == "workspace"
        self.query_one("#env-image", Input).display = not host_mode
        self.query_one("#env-image-label", Label).display = not host_mode
        picker = self.query_one("#config-workspace-picker", Button)
        picker.display = host_mode
        picker.disabled = locked or not host_mode
        self.query_one("#config-workspace-help", Static).update(
            "Choose an existing host project with the directory picker. Selection changes only this draft; "
            "save to apply it. The external project is never deleted with this chat."
            if host_mode else "Managed sandbox workspace (read-only path). New chats start empty here; "
            "only workspace/ is mounted, never conversation metadata or SynAI source. "
            "Deleting this conversation also permanently deletes its managed workspace files."
        )
        self.query_one("#env-runtime", Select).disabled = locked or host_mode
        self.query_one("#env-runtime", Select).display = not host_mode
        self.query_one("#env-runtime-label", Label).display = not host_mode
        self.query_one("#env-execution_mode", Select).disabled = locked
        self.query_one("#config-host-warning", Static).display = host_mode
        self.query_one("#config-limit-help", Static).update(
            "Timeouts are seconds; output bounds combined stdout/stderr bytes. Tool budgets pause for approval.\n"
            + ("HOST: container memory/CPU/PID limits below are retained but NOT enforced."
               if host_mode else "Memory/CPU/PID limits apply to managed containers.")
        )
        self.query_one("#environment-apply", Button).disabled = attached or self.sandbox_working or self.applying
        self.query_one("#environment-back", Button).disabled = self.sandbox_working or self.applying
        for button in self.query(".config-nav"):
            button.disabled = self.sandbox_working or self.applying
        self.update_overview()
        if not self.new:
            for identifier in ("config-create", "config-attach", "config-container"):
                widget = self.query_one(f"#{identifier}")
                widget.disabled = locked or host_mode
                widget.display = not host_mode
            enable = self.query_one("#config-host-enable", Button)
            enable.display = host_mode
            enable.disabled = locked or bool(
                self.coding_app.session and self.coding_app.host.matches(self.coding_app.session)
            )
            self.query_one("#config-sandbox-status", Static).update(Text(
                f"Attached: {self.coding_app.sandbox.container_id}\n"
                "Use DISCONNECT / REMOVE in the conversation before editing or replacing this sandbox."
                if attached else "Host access requires fresh confirmation; save draft changes first."
                if host_mode else "No sandbox attached. Uses the saved workspace, image, runtime and limits."
            ))

    def edited_environment(self) -> ConversationEnvironment:
        def value(key: str) -> str:
            return self.query_one(f"#env-{key}", Input).value

        host_mode = self.query_one("#env-execution_mode", Select).value == "host"
        try:
            workspace = (
                Path(value("workspace")).expanduser().resolve(strict=True) if host_mode
                else self.coding_app.storage.workspace(self.conversation_id)
            )
        except (OSError, ValueError) as exc:
            self.field_error("workspace", f"Workspace: {exc}")
        if host_mode and not workspace.is_dir():
            self.field_error("workspace", "Workspace must be an existing directory.")
        settings = self.initial_settings
        for key, label in self.FIELDS:
            if key == "workspace":
                continue
            try:
                parsed = (
                    int(value(key)) if key in {"output_bytes", "tool_budget", "pids"}
                    else float(value(key)) if key in {"command_timeout", "cpus"}
                    else value(key)
                )
                candidate = replace(settings, **{key: parsed})
                candidate.validate()
            except (ValueError, TypeError, OverflowError) as exc:
                self.field_error(key, f"{label}: {exc}")
            settings = candidate
        runtime = str(self.query_one("#env-runtime", Select).value)
        try:
            replace(settings, runtime=runtime).validate()
        except ValueError as exc:
            self.field_error("runtime", str(exc))
        settings = replace(settings, runtime=runtime)
        mode = self.query_one("#env-execution_mode", Select).value
        if mode not in {"host", "sandbox"}:
            self.field_error("execution_mode", "Execution mode must be sandbox or host")
        settings = replace(settings, execution_mode="host" if mode == "host" else "sandbox")
        settings.validate()
        return ConversationEnvironment.from_settings(settings, workspace)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id and event.button.id.startswith("config-nav-") and not self.applying and not self.sandbox_working:
            self.show_page(event.button.id.removeprefix("config-nav-"))
        elif event.button.id == "environment-back":
            self.action_back()
        elif event.button.id == "config-workspace-picker" and (
            not self.applying and not self.sandbox_working
            and (self.new or self.coding_app.sandbox.container_id is None)
            and self.query_one("#env-execution_mode", Select).value == "host"
        ):
            self.app.push_screen(DirectoryPicker(
                Path(self.query_one("#env-workspace", Input).value), self.initial_settings,
            ), self.directory_selected)
        elif event.button.id in {"config-create", "config-attach", "config-host-enable"} and not self.applying and not self.sandbox_working:
            self.sandbox_working = True
            self.refresh_sandbox_controls()
            self.run_worker(self.configure_sandbox(event.button.id), exclusive=True, group="config-sandbox")
        elif event.button.id == "environment-apply" and not self.applying and not self.sandbox_working:
            self.applying = True
            self.refresh_sandbox_controls()
            self.run_worker(self.apply(), exclusive=True, group="environment")

    def directory_selected(self, path: Path | None) -> None:
        if path is not None and self.is_mounted:
            self.host_workspace = str(path)
            self.query_one("#env-workspace", Input).value = str(path)
            self.update_overview()
        if self.is_mounted:
            self.query_one("#config-workspace-picker", Button).focus()

    async def configure_sandbox(self, action: str) -> None:
        app = self.coding_app
        try:
            if app.session is None or self.edited_environment() != app.session.environment:
                raise ValueError("Save environment changes before creating or attaching a sandbox.")
            app.setup_task = asyncio.create_task(
                app.enable_host_tools() if action == "config-host-enable" else app.setup_sandbox(
                    "create" if action == "config-create" else "attach", self.query_one("#config-container", Input).value,
                )
            )
            success = await app.setup_task
            self.query_one("#environment-result", Static).update(
                "Host tools enabled (not isolated)." if success and action == "config-host-enable"
                else "Sandbox ready for this conversation." if success else "Tool setup denied or unavailable; chat-only."
                + (f"\n{app.host_error}" if action == "config-host-enable" and app.host_error else "")
            )
        except (ValueError, OSError, SandboxError, TimeoutError, HistoryError) as exc:
            self.query_one("#environment-result", Static).update(Text(str(exc), style="synai.error"))
        finally:
            app.setup_task = None
            self.sandbox_working = False
            if self.is_mounted:
                self.refresh_sandbox_controls()
            app.controls()

    async def apply(self) -> None:
        try:
            environment = self.edited_environment()
            settings = environment.settings(self.coding_app.launch_settings)
            workspace = Path(environment.workspace)
            if self.new:
                environment.validate()
                self.coding_app.push_screen(ConversationMenu(
                    self.coding_app, self, environment=environment, preferred_model=self.preferred_model,
                ))
                return
            if not await self.coding_app.apply_environment(settings, workspace):
                self.query_one("#environment-result", Static).update("Change cancelled; saved environment unchanged.")
                return
            self.baseline = self.form_values()
            warning = self.coding_app.connection_error or (
                self.coding_app.host_error if environment.execution_mode == "host" else None
            )
            self.query_one("#environment-result", Static).update(Text(
                "Saved for this conversation. " + (warning or "Execution settings updated."),
                style="synai.warning" if warning else "synai.primary",
            ))
        except (ValueError, OSError, HistoryError, SandboxError, TimeoutError) as exc:
            self.query_one("#environment-result", Static).update(Text(str(exc), style="synai.error"))
        finally:
            self.applying = False
            if self.is_mounted:
                self.refresh_sandbox_controls()

    def action_back(self) -> None:
        if not self.applying and not self.sandbox_working:
            self.dismiss(None)


class ConversationDetails(MenuScreen[None]):
    BINDINGS = [("escape", "close", "Close")]
    INITIAL_FOCUS = "#details-close"
    displayed_details: str | None = None

    def __init__(self, app: CodingApp) -> None:
        super().__init__()
        self.coding_app = app

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-shell"):
            yield Label("CONVERSATION DETAILS", classes="modal-title")
            with MenuBody(classes="modal-body"):
                yield Static("", id="conversation-details")
            with Horizontal(classes="modal-actions"):
                yield Button("CLOSE", id="details-close")
            yield Static("PgUp/PgDn: scroll | Esc: close | F1: Help", classes="menu-hints")

    def on_mount(self) -> None:
        self.update_details()
        self.set_interval(0.2, self.update_details)

    def update_details(self) -> None:
        app = self.coding_app
        session = app.session
        if session is None:
            text = "No conversation selected."
        else:
            settings = app.settings
            text = (
                f"{session.title}\nModel: {session.model}\n{app.capability_status()}\n"
                f"{app.execution_status()}\n\nWorkspace: {session.workspace}\n"
                f"Storage: {app.storage.folder(session.session_id)}\n\n"
                f"Commands: {settings.command_timeout:g}s\nOutput: {settings.output_bytes // 1024} KiB\n"
                f"Tools: {settings.tool_budget} calls/turn\n"
                + (f"Runtime: {settings.runtime}\nImage: {settings.image}\n"
                   f"Resources: {settings.memory} RAM / {settings.cpus} CPUs / {settings.pids} PIDs"
                   if settings.execution_mode == "sandbox" else
                   "HOST: commands are not isolated; container resource limits do not apply.")
            )
        if text != self.displayed_details:
            self.query_one("#conversation-details", Static).update(Text(text))
            self.displayed_details = text

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


class HelpMenu(MenuScreen[None]):
    BINDINGS = [("escape", "back", "Back")]
    INITIAL_FOCUS = "#help-back"

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-shell"):
            yield Label("SynAI // HELP / SHORTCUTS", classes="modal-title")
            with MenuBody(classes="modal-body"):
                yield Static(
                "1. Main Menu / New Conversation: review settings, then choose a model.\n"
                "2. Conversations: highlight a row and Enter / Open to reopen.\n"
                "   Space / click checks rows; Delete Selected removes their managed files after confirmation.\n"
                "3. Conversation Settings / Execution: configure approved container or opt-in HOST tools.\n"
                "4. Review each proposed command, edit and network action.\n\n"
                "Conversation Settings: Overview, Execution, Workspace, Limits.\n"
                "F2 / Conversation Details: live status, full paths and resource limits.\n"
                "Main Menu / Connection Settings and Refresh Models: shared by all conversations.\n"
                "Changing a chat's model retains messages after confirmation; it never creates a chat.\n"
                "Ctrl+P / F2 Themes: dedicated theme picker, updating the whole UI.\n"
                "Arrows preview without saving; Enter / Apply saves; Escape restores the original.\n"
                "Save errors keep the picker open with Retry. F1 preserves the preview.\n"
                "Page focus opens it immediately; drafts stay intact. Save/Next validates every page.\n\n"
                "Execution chooses container or HOST (no sandbox) tools per conversation.\n"
                "Host commands are not isolated. Fresh host-access consent is required on resume.\n\n"
                "Ctrl+Enter / Ctrl+S: Send    Enter: Newline\n"
                "F1: Help / Shortcuts overlay    Escape: Close Help\n"
                "Shortcut hints are hidden from menu labels; shortcuts remain available.\n"
                "Escape / STOP: Cancel turn    F2: Dashboard / Return to chat\n"
                "F3: Composer    F4: Conversation    F5: Reasoning    F6: Activity\n"
                "F7 / Dashboard Workspace Editor: maximized Neovim + mini.nvim desktop window.\n"
                "F8: Select all prompt text while the composer is focused (F7 is reserved for the editor).\n"
                "Editor: Ctrl+Alt+1 files, Ctrl+Alt+2 Neovim, Ctrl+Alt+3 terminal, Ctrl+Alt+R refresh.\n"
                "Neovim AND terminal use this chat's HOST or validated SANDBOX environment.\n"
                "Sandbox editor requires an attached editor-capable image; never falls back to host.\n"
                "Manual terminal commands are not AI tools and do not have per-command approvals.\n"
                "Close the editor before switching chat/environment; quit offers Save/Discard/Cancel.\n"
                "Arrows: Move between menu controls by layout (no wrapping).\n"
                "Fields: arrows navigate until Enter starts editing.\n"
                "Enter / Escape finishes editing and keeps the draft, without saving.\n"
                "Inside open dropdowns/lists arrows select; Tab / Shift+Tab leaves them.\n"
                "Closed dropdown: arrows move focus; Enter opens. Open: arrows choose,\n"
                "Enter confirms, Escape cancels and returns focus to the dropdown.\n"
                "Menu arrows never operate scrollbars; PageUp/PageDown scroll details.\n"
                "In configuration, PageUp/PageDown scroll the active form, not the page rail.\n"
                "Moving focus automatically reveals an offscreen control.\n"
                "Tab / Shift+Tab: Focus controls    Enter / Space: Activate buttons\n"
                "Focused logs: Arrows, PageUp/PageDown, Home/End scroll.\n"
                "Dashboard: Alt+N New, Alt+C Conversations, Alt+M Model, Alt+S Settings,\n"
                "Alt+O Connection, Alt+R Refresh, Alt+D Disconnect, Alt+H Help (alias), Alt+X Stop.\n"
                "During responses F2 shows status and Stop; changes remain locked.\n"
                "Ctrl+Q: Quit    Ctrl+P: Theme picker\n\n"
                "Models without native tools remain chat-only. Reasoning is shown only when emitted.\n"
                "Network-enabled commands can expose workspace data; keep secrets out.\n"
                "Each chat is stored in ~/.synai/conversations/<id>. Only managed workspace/ is sandbox-mounted.\n"
                "Delete chat permanently deletes its managed files too; external host projects remain.\n"
                "If Ctrl+Enter is intercepted by VS Code, use Ctrl+S; see README for forwarding setup."
                )
            with Horizontal(classes="modal-actions"):
                yield Button("CLOSE", id="help-back")
            yield Static("PgUp/PgDn: scroll | Esc: close", classes="menu-hints")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_back()

    def action_back(self) -> None:
        self.dismiss(None)


class SandboxSwitch(MenuScreen[str]):
    BINDINGS = [("escape", "cancel", "Cancel switch")]
    INITIAL_FOCUS = "#switch-cancel"

    def __init__(self, container_id: str, owned: bool) -> None:
        super().__init__()
        self.container_id, self.owned = container_id, owned

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-shell"):
            yield Label("SWITCH CONVERSATION // SANDBOX", classes="modal-title")
            with MenuBody(classes="modal-body"):
                yield Static(Text(
                    f"Current container: {self.container_id}\n"
                    "Disconnect it before activating another conversation.\n"
                    "Workspace files are never deleted. The destination will need explicit create/attach approval."
                ))
            yield Button("CANCEL SWITCH", id="switch-cancel")
            yield Button("LEAVE RUNNING AND DISCONNECT", id="switch-leave", variant="warning")
            if self.owned:
                yield Button("REMOVE OWNED CONTAINER AND DISCONNECT", id="switch-remove", variant="error")
            yield Static("Enter: choose | Esc: cancel", classes="menu-hints")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss({"switch-leave": "leave", "switch-remove": "remove"}.get(event.button.id, "cancel"))

    def action_cancel(self) -> None:
        self.dismiss("cancel")
