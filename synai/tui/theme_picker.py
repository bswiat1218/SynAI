from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from synai.tui.navigation import MenuScreen

if TYPE_CHECKING:
    from synai.tui.application import CodingApp


class ThemePicker(MenuScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    INITIAL_FOCUS = "#theme-list"

    def __init__(self, app: CodingApp) -> None:
        super().__init__()
        self.coding_app = app
        self.original = app.theme
        self.selected = app.theme
        self.confirmed = False

    def compose(self) -> ComposeResult:
        app = self.coding_app
        names = [self.original, *sorted(name for name in app.available_themes if name != self.original)]
        with Vertical(classes="modal-shell"):
            yield Label("THEMES", classes="modal-title")
            yield Static("", id="theme-status")
            yield OptionList(*(
                Option(f"{name} // {'dark' if app.available_themes[name].dark else 'light'}", id=name)
                for name in names
            ), id="theme-list")
            yield Static("Normal text // theme foreground\nBorders and selection use theme accents.", id="theme-sample")
            yield Static("", id="theme-error")
            with Horizontal(classes="modal-actions"):
                yield Button("APPLY", id="theme-apply", variant="success")
                yield Button("CANCEL", id="theme-cancel")
            yield Static("Arrows: preview | Enter: apply | Esc: restore | F1: Help", classes="menu-hints")

    def on_mount(self) -> None:
        self.query_one("#theme-list", OptionList).highlighted = 0
        self.update_status()

    def update_status(self) -> None:
        self.query_one("#theme-status", Static).update(
            f"Original: {self.original}\nPreview: {self.selected} // Saved: {self.coding_app.preferences.theme}"
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option.id is not None:
            self.selected = event.option.id
            self.coding_app.theme = self.selected
            self.query_one("#theme-error", Static).update("")
            self.query_one("#theme-apply", Button).label = "APPLY"
            self.update_status()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.apply_selection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "theme-apply":
            self.apply_selection()
        else:
            self.action_cancel()

    def apply_selection(self) -> None:
        try:
            self.coding_app.save_selected_theme(self.selected)
        except (ValueError, OSError) as exc:
            self.query_one("#theme-error", Static).update(f"Theme previewed but not saved: {exc}")
            self.query_one("#theme-apply", Button).label = "RETRY"
            self.update_status()
            return
        self.confirmed = True
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_unmount(self) -> None:
        self.coding_app.finish_theme_preview(self.confirmed)
