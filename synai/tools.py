from __future__ import annotations

import difflib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from synai.execution_backend import ExecutionBackend
from synai.sandbox import SandboxError


Approval = Callable[[str, str], Awaitable[bool]]
PROPERTIES: dict[str, dict[str, str]] = {
    "list_files": {"path": "Directory relative to /workspace; use . for root"},
    "read_file": {"path": "Workspace-relative file path"},
    "write_file": {"path": "Workspace-relative file path", "content": "Complete UTF-8 file contents"},
    "patch_file": {"path": "Workspace-relative file path", "old": "Text to replace, exactly once", "new": "Replacement text"},
    "delete_file": {"path": "Workspace-relative file path to delete (not a directory)"},
    "terminal": {"command": "Noninteractive shell command; no sudo/escalation", "cwd": "Workspace-relative working directory"},
    "fetch_url": {"url": "HTTP(S) URL fetched from inside the sandbox"},
}
DESCRIPTIONS = {
    "list_files": "List one workspace directory.",
    "read_file": "Read a workspace text file.",
    "write_file": "Create or overwrite a workspace text file. Requires user approval.",
    "patch_file": "Replace an exact unique text match in a file. Requires approval.",
    "delete_file": "Delete one workspace file. Requires approval.",
    "terminal": "Run a bounded noninteractive shell command in the sandbox, including tests/git/builds. Requires approval.",
    "fetch_url": "Fetch an HTTP(S) resource in the sandbox. May transmit data; requires approval.",
}


def schemas(mode: str = "sandbox") -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {
            "name": name, "description": DESCRIPTIONS[name].replace(
                "in the sandbox", "directly on the host (not isolated)" if mode == "host" else "in the sandbox",
            ), "parameters": {
                "type": "object",
                "properties": {key: {"type": "string", "description": description.replace(
                    "/workspace", "the selected host workspace" if mode == "host" else "/workspace",
                ).replace("inside the sandbox", "on the host" if mode == "host" else "inside the sandbox")}
                    for key, description in fields.items()},
                "required": list(fields), "additionalProperties": False,
            },
        }} for name, fields in PROPERTIES.items()
    ]


class Tools:
    def __init__(self, sandbox: ExecutionBackend, approve: Approval) -> None:
        self.sandbox, self.approve = sandbox, approve

    async def call(self, name: str, arguments: Any) -> dict[str, Any]:
        if name not in PROPERTIES or not isinstance(arguments, dict):
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        expected = set(PROPERTIES[name])
        if set(arguments) != expected or any(not isinstance(value, str) for value in arguments.values()):
            return {"ok": False, "error": f"{name} requires exactly these string arguments: {', '.join(sorted(expected))}"}
        if len(json.dumps(arguments).encode()) > self.sandbox.settings.output_bytes * 2:
            return {"ok": False, "error": "Tool arguments exceed configured limit"}
        digest: str | None = None
        description = json.dumps(arguments, ensure_ascii=True, indent=2)
        try:
            if name in {"write_file", "patch_file", "delete_file"}:
                preview = await self.sandbox.execute("preview", {"path": arguments["path"]})
                if not preview.get("ok"):
                    return preview
                digest = preview["sha256"]
                old = preview["content"] or ""
                if name == "patch_file":
                    if not arguments["old"] or old.count(arguments["old"]) != 1:
                        return {"ok": False, "error": "Patch must match exactly once"}
                    new = old.replace(arguments["old"], arguments["new"], 1)
                else:
                    new = arguments["content"] if name == "write_file" else ""
                diff = "".join(difflib.unified_diff(
                    old.splitlines(keepends=True), new.splitlines(keepends=True),
                    fromfile=arguments["path"], tofile=arguments["path"],
                ))
                description = f"Target: {arguments['path']!r}\n\n{diff}"[:64000]
            if name not in {"read_file", "list_files"}:
                if self.sandbox.settings.execution_mode == "host":
                    description = f"HOST EXECUTION // NOT ISOLATED\nWorkspace: {self.sandbox.workspace}\n\n{description}"
                if not await self.approve(name, description):
                    return {"ok": False, "denied": True, "error": "User denied this action"}
            return await self.sandbox.execute(name, arguments, digest)
        except TimeoutError:
            return {"ok": False, "error": f"{self.sandbox.settings.execution_mode.title()} tool timed out"}
        except (SandboxError, OSError) as exc:
            return {"ok": False, "error": f"{self.sandbox.settings.execution_mode.title()} tool failed: {exc}"}
