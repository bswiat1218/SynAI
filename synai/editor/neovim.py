from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from synai.editor.environment import EditorContext, run
from synai.editor.protocol import EditorError


def expression(operation: str, data: dict[str, Any]) -> str:
    if operation not in {"theme", "open", "state", "save"}:
        raise EditorError("Unknown Neovim editor operation")
    encoded = json.dumps(data, ensure_ascii=True)
    # Vim single-quoted literals escape quotes by doubling; values never become code.
    literal = "'" + encoded.replace("'", "''") + "'"
    return f'luaeval("SynAI.call(_A[1], _A[2])", [{json.dumps(operation)}, json_decode({literal})])'


class Neovim:
    def __init__(self, context: EditorContext, directory: str) -> None:
        self.context = context
        self.directory = directory
        self.socket = str(Path(directory) / "nvim.sock")
        self.binary = str(Path(directory) / "nvim-linux-x86_64/bin/nvim")

    def spawn(self, role: str) -> list[str]:
        if role == "nvim":
            args = [
                "env", f"SYNAI_EDITOR_DIR={self.directory}",
                f"SYNAI_MINI_PATH={self.context.mini_path or str(Path(self.directory) / 'mini.nvim')}",
                f"NVIM_LOG_FILE={self.directory}/nvim.log",
                f"XDG_CONFIG_HOME={self.directory}/config",
                f"XDG_DATA_HOME={self.directory}/data",
                f"XDG_STATE_HOME={self.directory}/state",
                f"XDG_CACHE_HOME={self.directory}/cache",
                self.binary, "--noplugin", "-u", str(Path(self.directory) / "init.lua"),
                "--listen", self.socket, "-i", "NONE",
            ]
        else:
            args = [self.context.shell, "-i"]
        return self.context.command(
            "python3", str(Path(self.directory) / "supervisor.py"), "spawn",
            self.directory, role, *args, interactive=True,
        )

    def call(self, operation: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        output = run(
            self.context, self.binary, "--server", self.socket,
            "--remote-expr", expression(operation, data or {}),
        )
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise EditorError(f"Invalid Neovim reply: {output[:1024]}") from exc
        if not isinstance(result, dict):
            raise EditorError("Neovim reply must be an object")
        return result

    def jobs(self) -> list[int]:
        try:
            value = json.loads(run(
                self.context, "python3", str(Path(self.directory) / "supervisor.py"),
                "jobs", self.directory,
            ))
        except ValueError as exc:
            raise EditorError(f"Invalid terminal job response: {exc}") from exc
        if not isinstance(value, list) or any(type(pid) is not int for pid in value):
            raise EditorError("Invalid terminal job identities")
        return value
