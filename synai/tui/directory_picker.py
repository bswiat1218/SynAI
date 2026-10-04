from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from synai.config import Settings
from synai.execution_backend import validate_workspace
from synai.tui.navigation import MenuScreen


class DirectoryPicker(MenuScreen[Path | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, initial: Path, settings: Settings) -> None:
        super().__init__()
        self.initial = initial
        self.settings = settings
        self.current: Path | None = None
        self.directories: dict[str, Path] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="directory-picker", classes="modal-shell"):
            yield Label("CHOOSE HOST WORKSPACE", classes="modal-title")
            yield Static("Enter opens folders; Choose selects this directory.")
            yield Static("", id="directory-location")
            with Horizontal(id="directory-navigation", classes="modal-actions"):
                yield Button("UP", id="directory-up")
                yield Button("HOME", id="directory-home")
            yield OptionList(id="directory-list")
            yield Static("", id="directory-error")
            with Horizontal(id="directory-actions", classes="modal-actions"):
                yield Button("CHOOSE DIRECTORY", id="directory-choose", variant="success", disabled=True)
                yield Button("CANCEL", id="directory-cancel")
            yield Static("Enter: open / choose | Esc: cancel | F1: Help", classes="menu-hints")

    def on_mount(self) -> None:
        if not self.open_directory(self.initial):
            error = self.query_one("#directory-error", Static).render()
            if self.open_directory(Path.home()):
                self.query_one("#directory-error", Static).update(Text(
                    f"Starting directory unavailable; browsing home instead.\n{error}", style="synai.warning",
                ))
        self.query_one("#directory-list", OptionList).focus()

    def open_directory(self, path: Path) -> bool:
        try:
            directory = path.expanduser().resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("Choose an existing directory")
            children = sorted(
                (child for child in directory.iterdir() if child.is_dir()),
                key=lambda child: (child.name.casefold(), child.name),
            )
        except (OSError, ValueError) as exc:
            self.query_one("#directory-error", Static).update(Text(
                f"Cannot browse {path}: {exc}", style="synai.error",
            ))
            return False
        self.current = directory
        self.directories = {str(index): child for index, child in enumerate(children)}
        listing = self.query_one("#directory-list", OptionList)
        listing.clear_options()
        listing.add_options([
            Option(Text(f"{child.name}/" + (" [link]" if child.is_symlink() else "")), id=identifier)
            for identifier, child in self.directories.items()
        ])
        if children:
            listing.highlighted = 0
        self.query_one("#directory-location", Static).update(Text(str(directory)))
        self.query_one("#directory-up", Button).disabled = directory.parent == directory
        self.query_one("#directory-choose", Button).disabled = False
        self.query_one("#directory-error", Static).update(
            "" if children else "No subdirectories. Choose this folder or go Up."
        )
        return True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        identifier = event.option.id
        if identifier is not None and identifier in self.directories:
            self.open_directory(self.directories[identifier])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = event.button.id
        if action == "directory-cancel":
            self.action_cancel()
        elif action == "directory-home":
            self.open_directory(Path.home())
        elif action == "directory-up" and self.current is not None:
            self.open_directory(self.current.parent)
        elif action == "directory-choose" and self.current is not None:
            try:
                selected = validate_workspace(self.current, self.settings)
            except (OSError, ValueError) as exc:
                self.query_one("#directory-error", Static).update(Text(str(exc), style="synai.error"))
                return
            self.dismiss(selected)

    def action_cancel(self) -> None:
        self.dismiss(None)
