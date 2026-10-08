from __future__ import annotations

import asyncio
import difflib
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from synai.execution_backend import ExecutionBackend, validate_workspace
from synai.intelligence import IndexLimits, RepositoryIndex
from synai.models import Session
from synai.sandbox import SandboxError


Approval = Callable[[str, str], Awaitable[bool]]
ApprovalObserver = Callable[[str, str, bool | None], Awaitable[None]]
DispatchGuard = Callable[[], Awaitable[bool]]
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
INTELLIGENCE_ARGUMENTS: dict[str, tuple[str, dict[str, dict[str, Any]]]] = {
    "get_project_structure": ("Return a bounded structure of the active workspace.", {}),
    "find_symbol": ("Find Python symbols by exact name or qualified name.", {
        "name": {"type": "string", "description": "Exact symbol or qualified name", "maxLength": 512},
    }),
    "find_definition": ("Find exact Python symbol definitions without guessing ambiguous bindings.", {
        "name": {"type": "string", "description": "Exact symbol or qualified name", "maxLength": 512},
    }),
    "find_references": ("Find lexical Python AST references to a symbol.", {
        "name": {"type": "string", "description": "Exact symbol name, optionally qualified", "maxLength": 512},
    }),
    "find_callers": ("Find Python call expressions syntactically targeting a symbol name.", {
        "name": {"type": "string", "description": "Exact call target name, optionally qualified", "maxLength": 512},
    }),
    "find_implementations": ("Find direct classes with uniquely resolvable static inheritance from a class.", {
        "name": {"type": "string", "description": "Exact class or qualified name", "maxLength": 512},
    }),
    "find_imports": ("Find Python import statements matching a module, imported name, or alias.", {
        "query": {"type": "string", "description": "Exact module, imported name, or alias", "maxLength": 512},
    }),
    "find_tests": ("Find likely Python tests using test path and symbol naming conventions.", {}),
    "search_code": ("Search bounded UTF-8 source and text files in the active workspace.", {
        "query": {"type": "string", "description": "Literal text to search for", "maxLength": 1024},
    }),
    "get_diagnostics": ("Return Python parse/read errors and repository-index limit diagnostics.", {}),
}
INTELLIGENCE_TOOLS = frozenset(INTELLIGENCE_ARGUMENTS)


def schemas(mode: str = "sandbox") -> list[dict[str, Any]]:
    ordinary = [
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
    intelligence = [
        {"type": "function", "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }}
        for name, (description, properties) in INTELLIGENCE_ARGUMENTS.items()
    ]
    return ordinary + intelligence


class Tools:
    def __init__(self, sandbox: ExecutionBackend, approve: Approval) -> None:
        self.sandbox, self.approve = sandbox, approve
        self._indexes: dict[Path, RepositoryIndex] = {}

    async def call(
        self,
        name: str,
        arguments: Any,
        *,
        session: Session | None = None,
        approval_observer: ApprovalObserver | None = None,
        dispatch_guard: DispatchGuard | None = None,
    ) -> dict[str, Any]:
        if name not in PROPERTIES and name not in INTELLIGENCE_ARGUMENTS:
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        if name in INTELLIGENCE_ARGUMENTS:
            properties = INTELLIGENCE_ARGUMENTS[name][1]
            if set(arguments) != set(properties) or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > properties[key].get("maxLength", 4096)
                or any(character in value for character in ("/", "\\", "\x00"))
                for key, value in arguments.items()
            ):
                return {
                    "ok": False,
                    "error": f"{name} requires exactly these bounded string arguments: {', '.join(sorted(properties))}",
                }
        else:
            expected = set(PROPERTIES[name])
            if set(arguments) != expected or any(not isinstance(value, str) for value in arguments.values()):
                return {"ok": False, "error": f"{name} requires exactly these string arguments: {', '.join(sorted(expected))}"}
        if len(json.dumps(arguments).encode()) > self.sandbox.settings.output_bytes * 2:
            return {"ok": False, "error": "Tool arguments exceed configured limit"}
        if name in INTELLIGENCE_TOOLS:
            if session is None or not self.sandbox.matches(session):
                return {"ok": False, "error": "Repository intelligence requires the matching active conversation workspace"}
            return await self._intelligence(name, arguments)
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
                if approval_observer is not None:
                    await approval_observer(name, description, None)
                approved = await self.approve(name, description)
                if approval_observer is not None:
                    await approval_observer(name, description, approved)
                if not approved:
                    return {"ok": False, "denied": True, "error": "User denied this action"}
                if dispatch_guard is not None and not await dispatch_guard():
                    return {"ok": False, "denied": True, "error": "Action cancelled before dispatch"}
            return await self.sandbox.execute(name, arguments, digest)
        except TimeoutError:
            return {"ok": False, "error": f"{self.sandbox.settings.execution_mode.title()} tool timed out"}
        except (SandboxError, OSError) as exc:
            return {"ok": False, "error": f"{self.sandbox.settings.execution_mode.title()} tool failed: {exc}"}

    async def _intelligence(self, name: str, arguments: dict[str, str]) -> dict[str, Any]:
        workspace = self.sandbox.workspace
        if workspace is None:
            return {"ok": False, "error": "Repository intelligence requires an active validated workspace"}
        try:
            root = validate_workspace(
                workspace,
                self.sandbox.settings,
                sandbox=self.sandbox.settings.execution_mode == "sandbox",
            )
            limits = IndexLimits(max_output_bytes=min(524_288, self.sandbox.settings.output_bytes))
            index = self._indexes.get(root)
            if index is None or index.limits != limits:
                index = RepositoryIndex(root, limits)
            self._indexes = {root: index}
            cancellation = threading.Event()

            def run_query() -> dict[str, Any]:
                try:
                    return index.query(name, arguments, cancellation)
                except InterruptedError:
                    return {"ok": False, "error": "Repository intelligence query cancelled"}

            worker = asyncio.create_task(asyncio.to_thread(run_query))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancellation.set()
                try:
                    await asyncio.shield(worker)
                except InterruptedError:
                    pass
                raise
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"Repository intelligence workspace is unavailable: {exc}"}
