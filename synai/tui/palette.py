from __future__ import annotations

from rich.theme import Theme as RichTheme
from rich.style import Style
from textual.color import Color
from textual.theme import Theme

from synai.preferences import DEFAULT_THEME


CYBERPUNK = Theme(
    name=DEFAULT_THEME, primary="#00f5ff", secondary="#ff4fcb", accent="#955cff",
    warning="#ffe66d", error="#ff709e", success="#00c9d6", foreground="#e8defa",
    background="#090516", surface="#140d26", panel="#211033", dark=True,
)


def rich_theme(variables: dict[str, str]) -> RichTheme:
    foreground = Color.parse(variables["foreground"]).rich_color
    return RichTheme({
        f"synai.{name}": Style(color=foreground, bold=name in {"warning", "error"})
        for name in ("text", "primary", "secondary", "accent", "warning", "error", "muted")
    })
