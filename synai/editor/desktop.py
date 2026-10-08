from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Vte

# Load GTK from the chosen interpreter before exposing the SynAI package directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from synai.editor.environment import EditorContext, cleanup, prepare, run
from synai.editor.neovim import Neovim
from synai.editor.protocol import MAX_MESSAGE, EditorError, decode, encode, palette


class Desktop:
    def __init__(self) -> None:
        self.context: EditorContext | None = None
        self.nvim: Neovim | None = None
        self.directory = ""
        self.ready = False
        self.closing = False
        self.parent_connected = True
        self.exited = False
        self.nvim_alive = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.colors: dict[str, Any] = {}
        self.spawned: set[str] = set()
        self.launch_id = 0
        self.window = Gtk.Window(title="SynAI // Workspace Editor")
        self.window.set_default_size(1200, 800)
        self.window.connect("delete-event", self.on_delete)
        self.window.connect("key-press-event", self.on_key)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.window.add(box)
        self.banner = Gtk.Label(label="Starting workspace editor...")
        self.banner.set_xalign(0)
        self.banner.set_line_wrap(True)
        self.banner.set_max_width_chars(100)
        box.pack_start(self.banner, False, False, 4)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)

        self.tree_store = Gtk.TreeStore(str, str, bool, bool)
        self.tree = Gtk.TreeView(model=self.tree_store)
        renderer = Gtk.CellRendererText()
        self.tree.append_column(Gtk.TreeViewColumn("WORKSPACE", renderer, text=0))
        self.tree.connect("row-expanded", self.expand)
        self.tree.connect("row-activated", self.activate_file)
        self.tree.connect("key-press-event", self.tree_key)
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.tree)
        scroller.set_size_request(200, -1)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.refresh_button = Gtk.Button(label="REFRESH FILES")
        self.refresh_button.connect("clicked", lambda _button: self.refresh())
        side.pack_start(self.refresh_button, False, False, 0)
        side.pack_start(scroller, True, True, 0)

        self.editor = Vte.Terminal()
        self.terminal = Vte.Terminal()
        self.editor.set_scrollback_lines(0)
        self.terminal.set_scrollback_lines(5000)
        self.editor.connect("child-exited", self.child_exit, "nvim")
        self.terminal.connect("child-exited", self.child_exit, "shell")
        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.add(self.editor)
        terminal_scroll = Gtk.ScrolledWindow()
        terminal_scroll.add(self.terminal)
        terminal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_label = Gtk.Label(label="TERMINAL")
        self.terminal_label.set_xalign(0)
        terminal_box.pack_start(self.terminal_label, False, False, 0)
        terminal_box.pack_start(terminal_scroll, True, True, 0)
        vertical = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        vertical.pack1(editor_scroll, True, False)
        vertical.pack2(terminal_box, True, False)
        vertical.set_position(550)
        horizontal = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        horizontal.pack1(side, False, False)
        horizontal.pack2(vertical, True, False)
        horizontal.set_position(250)
        box.pack_start(horizontal, True, True, 0)
        box.pack_start(self.status, False, False, 4)
        hints = Gtk.Label(label="Ctrl+Alt+1 Files | Ctrl+Alt+2 Neovim | "
                          "Ctrl+Alt+3 Terminal | Ctrl+Alt+R Refresh | normal Neovim keys")
        box.pack_start(hints, False, False, 4)
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            self.window.get_screen(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.set_editable(False)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.startup_abort)

    def send(self, message: dict[str, Any]) -> None:
        if not self.parent_connected:
            return
        try:
            sys.stdout.buffer.write(encode(message))
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            self.parent_connected = False

    def reply(self, identifier: int, **values: Any) -> None:
        self.send({"type": "reply", "id": identifier, "ok": True, **values})

    def error(self, text: str, identifier: int | None = None) -> None:
        self.status.set_text(text)
        if identifier is not None:
            self.send({"type": "reply", "id": identifier, "ok": False, "error": text})
        else:
            self.send({"type": "error", "error": text})

    def work(self, action: Callable[[], Any], done: Callable[[Any], None],
             failed: Callable[[str], None] | None = None) -> None:
        def deliver(callback: Callable[[Any], None], value: Any) -> bool:
            if not self.exited:
                try:
                    callback(value)
                except (EditorError, OSError, ValueError, TypeError, GLib.Error) as exc:
                    if not self.ready:
                        self.launch_failed(str(exc))
                    else:
                        self.error(str(exc))
            return False

        def complete(future) -> None:
            try:
                result = future.result()
            except (EditorError, OSError, ValueError, TypeError) as exc:
                GLib.idle_add(deliver, failed or self.error, str(exc))
            else:
                GLib.idle_add(deliver, done, result)

        self.executor.submit(action).add_done_callback(complete)

    def receive(self, message: dict[str, Any]) -> bool:
        identifier = message.get("id")
        if type(identifier) is not int:
            self.error("Editor request needs a numeric identity")
            return False
        try:
            kind = message["type"]
            if kind == "launch":
                if self.context is not None:
                    raise EditorError("Workspace editor is already initialized")
                self.context = EditorContext.from_payload(message.get("context"))
                self.colors = palette(message.get("palette"))
                self.launch_id = identifier
                self.work(lambda: prepare(self.require_context()), self.prepared,
                          lambda error: self.launch_failed(error))
            elif kind == "focus":
                self.window.present()
                self.reply(identifier)
            elif kind == "theme":
                colors = palette(message.get("palette"))
                if self.closing:
                    raise EditorError("Workspace editor is closing")
                self.work(lambda: self.require_nvim().call("theme", colors),
                          lambda _value: self.theme_done(colors, identifier),
                          lambda error: self.error(error, identifier))
            elif kind == "close":
                self.close(identifier)
            else:
                raise EditorError("Unknown editor request")
        except (EditorError, OSError, ValueError, TypeError) as exc:
            self.error(str(exc), identifier)
        return False

    def require_context(self) -> EditorContext:
        if self.context is None:
            raise EditorError("Workspace editor has no environment")
        return self.context

    def require_nvim(self) -> Neovim:
        if self.nvim is None:
            raise EditorError("Neovim is not initialized")
        return self.nvim

    def prepared(self, directory: str) -> None:
        self.directory = directory
        self.nvim = Neovim(self.require_context(), directory)
        context = self.require_context()
        label = "HOST // NOT ISOLATED" if context.mode == "host" else f"SANDBOX // {context.container[:12]}"
        self.banner.set_text(f"{label} // {context.workspace}")
        self.terminal_label.set_text(f"INTERACTIVE TERMINAL // {label}")
        self.apply_colors(self.colors)
        self.window.maximize()
        self.window.show_all()
        self.refresh()
        for role, terminal in (("nvim", self.editor), ("shell", self.terminal)):
            # VTE's GI binding needs positional arguments, including child_setup_data.
            terminal.spawn_async(
                Vte.PtyFlags.DEFAULT, context.workspace, self.nvim.spawn(role),
                ["TERM=xterm-256color", "COLORTERM=truecolor", "NVIM=", "NVIM_LISTEN_ADDRESS="],
                GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, self.on_spawn, role,
            )

    def on_spawn(self, terminal, pid: int, error, role: str) -> None:
        if error is not None:
            self.launch_failed(f"Cannot start {role}: {error}")
            return
        self.spawned.add(role)
        if role == "nvim":
            self.nvim_alive = True
        if self.spawned == {"nvim", "shell"}:
            def wait_ready() -> None:
                deadline = time.monotonic() + 12
                last = ""
                while time.monotonic() < deadline:
                    try:
                        self.require_nvim().call("theme", self.colors)
                        self.require_nvim().call("state")
                        return
                    except EditorError as exc:
                        last = str(exc)
                        time.sleep(0.15)
                raise EditorError(f"Neovim did not become ready: {last}")
            self.work(wait_ready, self.started, self.launch_failed)

    def started(self, _value: Any) -> None:
        self.ready = True
        self.set_editable(True)
        self.status.set_text("Ready // Manual commands are not AI tool requests")
        self.editor.grab_focus()
        self.reply(self.launch_id, recovery=self.directory)

    def launch_failed(self, error: str) -> None:
        self.error(error + (f" // Recovery: {self.directory}" if self.directory else ""), self.launch_id)
        if self.directory:
            self.work(lambda: self.stop_processes(), lambda _value: self.finish(),
                      lambda failure: self.cleanup_failed(failure))
        else:
            self.finish()

    def theme_done(self, colors: dict[str, Any], identifier: int) -> None:
        self.colors = colors
        self.apply_colors(colors)
        self.reply(identifier)

    def apply_colors(self, colors: dict[str, Any]) -> None:
        css = (
            f"window, treeview, label {{color:{colors['foreground']};background-color:{colors['background']};}}"
            f"button {{color:{colors['foreground']};background-image:none;background-color:{colors['panel']};"
            f"border-color:{colors['primary']};}}"
            f"treeview:selected {{color:{colors['foreground']};background-color:{colors['panel']};}}"
            f"*:focus {{outline-color:{colors['primary']};}}"
        )
        self.css.load_from_data(css.encode())

        def rgba(color: str):
            value = Gdk.RGBA()
            value.parse(color)
            return value

        base = [colors[key] for key in (
            "background", "error", "success", "warning", "primary", "secondary", "accent", "foreground")]
        for terminal in (self.editor, self.terminal):
            terminal.set_colors(rgba(colors["foreground"]), rgba(colors["background"]),
                                [rgba(color) for color in base * 2])
            terminal.set_color_cursor(rgba(colors["primary"]))
            terminal.set_color_highlight(rgba(colors["panel"]))
            terminal.set_color_highlight_foreground(rgba(colors["foreground"]))

    def refresh(self) -> None:
        if self.context is None or self.closing:
            return
        self.tree_store.clear()
        root = self.tree_store.append(None, ["workspace", self.context.workspace, True, False])
        self.tree_store.append(root, ["Loading...", "", False, True])
        self.tree.expand_row(self.tree_store.get_path(root), False)

    def expand(self, tree, iterator, path) -> None:
        if self.tree_store[iterator][3]:
            return
        self.tree_store[iterator][3] = True
        reference = Gtk.TreeRowReference.new(self.tree_store, path)
        directory = self.tree_store[iterator][1]
        root = Path(self.require_context().workspace).resolve()

        def listing() -> list[tuple[str, str, bool]]:
            folder = Path(directory).resolve(strict=True)
            if not folder.is_relative_to(root):
                raise EditorError("Folder is outside the workspace")
            entries = []
            for count, entry in enumerate(folder.iterdir()):
                if count >= 2000:
                    raise EditorError("Folder contains more than 2000 entries; use Neovim to navigate it")
                target = entry.resolve()
                if not target.is_relative_to(root) or (entry.is_symlink() and entry.is_dir()):
                    continue
                entries.append((entry.name, str(entry), entry.is_dir()))
            return sorted(entries, key=lambda row: (not row[2], row[0].casefold()))

        def populate(entries) -> None:
            current = reference.get_path()
            if current is None:
                return
            node = self.tree_store.get_iter(current)
            while child := self.tree_store.iter_children(node):
                self.tree_store.remove(child)
            for name, full, directory_flag in entries:
                child = self.tree_store.append(node, [name, full, directory_flag, False])
                if directory_flag:
                    self.tree_store.append(child, ["Loading...", "", False, True])
            if self.status.get_text().startswith("File tree:"):
                self.status.set_text("File tree refreshed")

        def failed(error: str) -> None:
            current = reference.get_path()
            if current is not None:
                node = self.tree_store.get_iter(current)
                self.tree_store[node][3] = False
            self.error(f"File tree: {error}")

        self.work(listing, populate, failed)

    def activate_file(self, tree, path, column) -> None:
        node = self.tree_store.get_iter(path)
        if self.tree_store[node][2]:
            if tree.row_expanded(path):
                tree.collapse_row(path)
            else:
                tree.expand_row(path, False)
        elif self.tree_store[node][1] and self.ready and not self.closing:
            try:
                target = self.require_context().file_path(self.tree_store[node][1])
            except (EditorError, OSError, ValueError) as exc:
                self.error(str(exc))
                return
            self.work(lambda: self.require_nvim().call("open", {"path": target}),
                      lambda _value: self.editor.grab_focus())

    def tree_key(self, tree, event) -> bool:
        if event.keyval == Gdk.KEY_F5:
            self.refresh()
            return True
        return False

    def on_key(self, window, event) -> bool:
        modifiers = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        if event.state & modifiers != modifiers:
            return False
        targets = {Gdk.KEY_1: self.tree, Gdk.KEY_2: self.editor, Gdk.KEY_3: self.terminal}
        if event.keyval in targets:
            targets[event.keyval].grab_focus()
            return True
        if event.keyval in {Gdk.KEY_r, Gdk.KEY_R}:
            self.refresh()
            return True
        return False

    def on_delete(self, _window, _event) -> bool:
        self.close(None)
        return True

    def close(self, identifier: int | None) -> None:
        if not self.ready or self.closing:
            self.error("Wait for the editor operation to finish", identifier)
            return
        self.closing = True
        self.set_editable(False)
        self.window.present()

        def state() -> dict[str, Any]:
            modified = self.require_nvim().call("state")["modified"] if self.nvim_alive else []
            return {"modified": modified, "jobs": self.require_nvim().jobs()}

        self.work(state, lambda value: self.confirm_close(value, identifier),
                  lambda error: self.close_failed(error, identifier))

    def confirm_close(self, state: dict[str, Any], identifier: int | None) -> None:
        modified = state["modified"]
        if modified:
            dialog = Gtk.MessageDialog(
                transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                text="Save modified Neovim buffers before closing?",
            )
            dialog.format_secondary_text("\n".join(item["name"] or "[Unnamed buffer]" for item in modified))
            dialog.add_button("CANCEL", Gtk.ResponseType.CANCEL)
            dialog.add_button("DISCARD", Gtk.ResponseType.REJECT)
            dialog.add_button("SAVE", Gtk.ResponseType.ACCEPT)
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            response = dialog.run()
            dialog.destroy()
            if response == Gtk.ResponseType.ACCEPT:
                paths = {}
                for item in modified:
                    if not item["name"]:
                        path = self.save_destination()
                        if path is None:
                            self.cancel_close(identifier)
                            return
                        paths[str(item["buffer"])] = path
                def save() -> None:
                    self.require_nvim().call("save", {"paths": paths})
                    if self.require_nvim().call("state")["modified"]:
                        raise EditorError("Buffers remain modified after saving; inspect them before closing")
                self.work(save,
                          lambda _value: self.confirm_jobs(state["jobs"], identifier),
                          lambda error: self.close_failed(error, identifier))
                return
            if response != Gtk.ResponseType.REJECT:
                self.cancel_close(identifier)
                return
        self.confirm_jobs(state["jobs"], identifier)

    def save_destination(self) -> str | None:
        context = self.require_context()
        dialog = Gtk.FileChooserDialog(
            title="Save unnamed Neovim buffer in workspace", parent=self.window,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons("CANCEL", Gtk.ResponseType.CANCEL, "SAVE", Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_folder(context.workspace)
        response = dialog.run()
        name = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or name is None:
            return None
        target = Path(name).resolve()
        root = Path(context.workspace).resolve()
        if not target.is_relative_to(root):
            self.error("Save destination must be inside the workspace")
            return None
        return str(Path(context.cwd) / target.relative_to(root))

    def confirm_jobs(self, jobs: list[int], identifier: int | None) -> None:
        if jobs:
            dialog = Gtk.MessageDialog(
                transient_for=self.window, modal=True, message_type=Gtk.MessageType.WARNING,
                text="Stop editor terminal jobs and close?",
            )
            dialog.format_secondary_text(
                "Running editor-owned jobs will be terminated. Deliberately detached jobs may survive.")
            dialog.add_button("CANCEL", Gtk.ResponseType.CANCEL)
            dialog.add_button("STOP AND CLOSE", Gtk.ResponseType.ACCEPT)
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.cancel_close(identifier)
                return
        self.work(lambda: cleanup(self.require_context(), self.directory),
                  lambda _value: self.closed(identifier),
                  lambda error: self.close_failed(error, identifier))

    def cancel_close(self, identifier: int | None) -> None:
        self.closing = False
        self.set_editable(True)
        if identifier is not None:
            self.reply(identifier, cancelled=True)

    def close_failed(self, error: str, identifier: int | None) -> None:
        self.closing = False
        self.set_editable(True)
        self.error(f"Editor close failed: {error} // Recovery: {self.directory}", identifier)

    def set_editable(self, enabled: bool) -> None:
        for widget in (self.editor, self.terminal, self.tree, self.refresh_button):
            widget.set_sensitive(enabled)

    def closed(self, identifier: int | None) -> None:
        if identifier is not None:
            self.reply(identifier, cancelled=False)
        self.send({"type": "closed"})
        self.finish()

    def stop_processes(self) -> None:
        run(self.require_context(), "python3", str(Path(self.directory) / "supervisor.py"),
            "stop", self.directory)

    def cleanup_failed(self, error: str) -> None:
        self.error(f"Editor cleanup failed: {error} // Recovery: {self.directory}")
        self.finish()

    def child_exit(self, terminal, code: int, role: str) -> None:
        if self.closing or self.exited:
            return
        if role == "nvim":
            self.nvim_alive = False
            if self.ready:
                if code == 0:
                    self.close(None)
                else:
                    self.error(f"Neovim exited ({code}) // Recovery retained: {self.directory}")
                    self.work(self.stop_processes, lambda _value: self.finish(), self.cleanup_failed)
        else:
            self.terminal_label.set_text(f"TERMINAL EXITED ({code}) // Close and reopen editor to restart")
            self.error(f"Interactive terminal exited ({code})")

    def parent_eof(self) -> bool:
        self.parent_connected = False
        self.banner.set_text("SynAI disconnected // Close this window to save or discard buffers")
        if self.ready:
            self.close(None)
        else:
            self.startup_abort()
        return False

    def startup_abort(self) -> bool:
        if self.ready:
            self.parent_eof()
        elif self.directory:
            self.work(self.stop_processes, lambda _value: self.finish(), self.cleanup_failed)
        else:
            self.finish()
        return False

    def finish(self) -> None:
        self.exited = True
        self.window.destroy()
        self.executor.shutdown(wait=False)
        if Gtk.main_level():
            Gtk.main_quit()

    def read_input(self) -> None:
        while data := sys.stdin.buffer.readline(MAX_MESSAGE + 1):
            try:
                message = decode(data)
            except EditorError as exc:
                GLib.idle_add(self.error, str(exc))
                continue
            GLib.idle_add(self.receive, message)
        GLib.idle_add(self.parent_eof)


def main() -> None:
    desktop = Desktop()
    threading.Thread(target=desktop.read_input, daemon=True).start()
    Gtk.main()


if __name__ == "__main__":
    main()
