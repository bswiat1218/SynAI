from __future__ import annotations

from typing import Literal, TypeVar

from textual.binding import Binding
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList, Select, TextArea
from textual.widgets._select import SelectCurrent, SelectOverlay

Direction = Literal["up", "down", "left", "right"]
Result = TypeVar("Result")


class MenuBody(VerticalScroll, can_focus=False):
    pass


class MenuInput(Input):
    editing = reactive(False)
    BINDINGS = [
        Binding("enter", "toggle_edit", "Edit field", show=False, priority=True),
        Binding("escape", "finish_edit", "Finish editing", show=False, priority=True),
    ]

    def watch_editing(self, editing: bool) -> None:
        self.set_class(editing, "editing")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_edit":
            return True
        if action == "finish_edit":
            return self.editing
        if not self.editing:
            return False
        return super().check_action(action, parameters)

    def begin_edit(self) -> None:
        if not self.disabled:
            self.editing = True
            self.action_select_all()

    def action_toggle_edit(self) -> None:
        if self.editing:
            self.editing = False
        else:
            self.begin_edit()

    def action_finish_edit(self) -> None:
        self.editing = False

    async def _on_key(self, event: events.Key) -> None:
        if self.editing:
            await super()._on_key(event)
        elif event.is_printable:
            event.stop()
        event.prevent_default()

    def _on_paste(self, event: events.Paste) -> None:
        if self.editing:
            super()._on_paste(event)
        event.stop()
        event.prevent_default()

    def _on_blur(self, event: events.Blur) -> None:
        if self.app.app_focus and self.app.screen is self.screen:
            self.editing = False
        super()._on_blur(event)
        event.prevent_default()


class MenuSelectOverlay(SelectOverlay):
    def _on_blur(self, event: events.Blur) -> None:
        # Terminal blur is not a user dismissal of the dropdown.
        if not self.app.app_focus:
            event.prevent_default()
            return
        super()._on_blur(event)
        event.prevent_default()


class MenuSelect(Select):
    BINDINGS = [
        Binding("enter", "show_overlay", "Open dropdown", show=False),
        Binding("space", "ignore_space", show=False),
    ]

    def action_ignore_space(self) -> None:
        pass

    def compose(self) -> ComposeResult:
        yield SelectCurrent(self.prompt)
        yield MenuSelectOverlay(type_to_search=self._type_to_search).data_bind(compact=Select.compact)


def directional_score(
    origin: Region, target: Region, direction: Direction,
) -> tuple[int, float, float] | None:
    """Prefer aligned controls, then the closest edge in the requested direction."""
    vertical = direction in {"up", "down"}
    start, end = (origin.y, origin.bottom) if vertical else (origin.x, origin.right)
    other_start, other_end = (target.y, target.bottom) if vertical else (target.x, target.right)
    cross_start, cross_end = (origin.x, origin.right) if vertical else (origin.y, origin.bottom)
    other_cross_start, other_cross_end = (
        (target.x, target.right) if vertical else (target.y, target.bottom)
    )
    forward = direction in {"down", "right"}
    if forward:
        if other_start < end:
            return None
        gap = other_start - end
    else:
        if other_end > start:
            return None
        gap = start - other_end
    overlap = min(cross_end, other_cross_end) - max(cross_start, other_cross_start)
    cross_gap = max(0, other_cross_start - cross_end, cross_start - other_cross_end)
    center_distance = abs(cross_start + cross_end - other_cross_start - other_cross_end) / 2
    return (0 if overlap > 0 else 1, gap + cross_gap, center_distance)


class MenuScreen(ModalScreen[Result]):
    last_control: Widget | None = None
    INITIAL_FOCUS: str | None = None
    BINDINGS = [
        Binding(key, f"navigate('{key}')", "Move focus", show=False, priority=True)
        for key in ("up", "down", "left", "right")
    ] + [
        Binding("enter", "recover_focus", show=False, priority=True),
        Binding("pageup", "body_scroll(-1)", show=False, priority=True),
        Binding("pagedown", "body_scroll(1)", show=False, priority=True),
    ]

    def on_mount(self) -> None:
        self.call_after_refresh(self.initialize_focus)

    def on_resize(self) -> None:
        self.set_class(self.size.width < 70, "compact-menu")

    def initialize_focus(self) -> None:
        if self.INITIAL_FOCUS is not None:
            target = self.query_one(self.INITIAL_FOCUS)
            if target in self.focus_chain:
                target.focus()
                return
        self.recover_focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, (Button, Input, Select, OptionList, TextArea)):
            self.last_control = event.widget

    def recover_focus(self) -> Widget | None:
        controls = [
            widget for widget in self.focus_chain
            if isinstance(widget, (Button, Input, Select, OptionList, TextArea))
        ]
        if self.focused in controls:
            return self.focused
        last = self.last_control
        target = last if last in controls else next(
            (widget for widget in controls if widget.id in {"deny", "switch-cancel"}), None,
        )
        if target is None and controls:
            target = controls[0]
        if target is not None:
            self.set_focus(target, scroll_visible=False, from_app_focus=True)
        return target

    def action_recover_focus(self) -> None:
        # A recovery Enter only restores focus; it cannot accidentally grant consent.
        self.recover_focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "body_scroll":
            return not isinstance(self.focused, (OptionList, TextArea))
        if action == "recover_focus":
            return self.app.screen is self and self.focused is None
        if action == "navigate":
            if self.app.screen is not self:
                return False
            direction = parameters[0]
            focused = self.focused
            if focused is not None:
                for node in focused.ancestors_with_self:
                    if isinstance(node, TextArea):
                        return False
                    if isinstance(node, Input):
                        return not isinstance(node, MenuInput) or not node.editing or direction not in {"left", "right"}
                    if isinstance(node, Select):
                        return not node.expanded or direction not in {"up", "down"}
                    if isinstance(node, OptionList):
                        return direction not in {"up", "down"}
            return True
        return super().check_action(action, parameters)

    async def action_navigate(self, direction: Direction) -> None:
        if self.app.screen is not self:
            return
        recovering = self.focused is None
        origin = self.focused if not recovering else self.recover_focus()
        if isinstance(origin, MenuInput) and origin.editing and direction not in {"left", "right"}:
            return
        if recovering:
            if isinstance(origin, OptionList) and direction in {"up", "down"}:
                await self.app.run_action(f"cursor_{direction}", default_namespace=origin)
                return
            if isinstance(origin, MenuInput) and origin.editing and direction in {"left", "right"}:
                await self.app.run_action(f"cursor_{direction}", default_namespace=origin)
                return
        if not isinstance(origin, (Button, Select, MenuInput)) or isinstance(origin, Select) and origin.expanded:
            return
        body = next((node for node in origin.ancestors if isinstance(node, MenuBody)), None)
        targets: list[tuple[tuple[int, int, float, float], int, Widget]] = []
        for index, widget in enumerate(self.focus_chain):
            if widget is origin or not isinstance(widget, (Button, Input, Select, OptionList, TextArea)):
                continue
            region = widget.region
            if region.width <= 0 or region.height <= 0:
                continue
            score = directional_score(origin.region, region, direction)
            if score is not None:
                scope = 0 if body is None or body in widget.ancestors else 1
                targets.append(((scope, *score), index, widget))
        if targets:
            _, _, target = min(targets, key=lambda item: (item[0], item[1]))
            target.focus()
            target.scroll_visible(animate=False)

    def body_for_scroll(self) -> MenuBody | None:
        focused = self.focused
        body = next(
            (node for node in focused.ancestors if isinstance(node, MenuBody)), None,
        ) if focused is not None else None
        if body is None:
            body = next((node for node in self.query(MenuBody) if node.region.width and node.region.height), None)
        return body

    def action_body_scroll(self, direction: int) -> None:
        body = self.body_for_scroll()
        if body is not None:
            if direction > 0:
                body.action_page_down()
            else:
                body.action_page_up()
