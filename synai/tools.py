from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from synai.execution_backend import ExecutionBackend, validate_workspace
from synai.coding_agent.git import (
    GIT_ARGUMENTS,
    GIT_DESCRIPTIONS,
    GitInspector,
    validate_git_arguments,
)
from synai.coding_agent.checkpoints import CheckpointManager, PreparedRestore
from synai.coding_agent.policies import (
    AutonomyMode,
    AutonomyPolicy,
    PolicyDecisionType,
    PolicyReason,
    PolicyRequest,
    argument_fingerprint,
    classify_operation,
)
from synai.intelligence import IndexLimits, RepositoryIndex
from synai.models import Session
from synai.sandbox import SandboxError


Approval = Callable[[str, str], Awaitable[bool]]
ApprovalObserver = Callable[[str, str, bool | None], Awaitable[None]]
DispatchGuard = Callable[[], Awaitable[bool]]
MutationObserver = Callable[
    [str, str, dict[str, Any], dict[str, Any] | None],
    Awaitable[dict[str, Any] | None],
]
ToolEventObserver = Callable[[str, str | None], Awaitable[None]]
_logger = logging.getLogger(__name__)
CHECKPOINT_ARGUMENTS: dict[str, dict[str, dict[str, Any]]] = {
    "git_checkpoint": {
        "task_id": {
            "type": "string",
            "description": "Optional active Agent Task identifier; empty when not task-owned",
            "maxLength": 128,
        },
        "paths": {
            "type": "array",
            "items": {"type": "string", "maxLength": 512},
            "description": "Workspace-relative files to snapshot",
            "minItems": 1,
            "maxItems": 64,
        },
        "require_complete": {
            "type": "boolean",
            "description": "Reject creation unless every requested file is captured",
        },
    },
    "restore_checkpoint": {
        "checkpoint_id": {
            "type": "string",
            "description": "Checkpoint identifier returned by git_checkpoint",
            "pattern": "^[a-f0-9]{32}$",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string", "maxLength": 512},
            "description": "Explicit subset of checkpoint files to restore",
            "minItems": 1,
            "maxItems": 64,
        },
    },
}


def _consume_worker_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        _logger.exception("Checkpoint preflight worker failed after its caller stopped waiting")
CHECKPOINT_DESCRIPTIONS = {
    "git_checkpoint": "Create an explicitly approved private workspace snapshot; this does not create a Git commit.",
    "restore_checkpoint": "Explicitly restore captured file contents after conflict preflight; this does not reset Git.",
}
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


def schemas(
    mode: str = "sandbox",
    *,
    include_git: bool = False,
    include_checkpoints: bool = False,
) -> list[dict[str, Any]]:
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
    git = [
        {"type": "function", "function": {
            "name": name,
            "description": GIT_DESCRIPTIONS[name],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }}
        for name, properties in GIT_ARGUMENTS.items()
    ]
    checkpoints = [
        {"type": "function", "function": {
            "name": name,
            "description": CHECKPOINT_DESCRIPTIONS[name],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }}
        for name, properties in CHECKPOINT_ARGUMENTS.items()
    ]
    return ordinary + intelligence + (
        git if include_git else []
    ) + (checkpoints if include_checkpoints else [])


def validate_checkpoint_arguments(name: str, arguments: dict[str, Any]) -> bool:
    properties = CHECKPOINT_ARGUMENTS[name]
    if set(arguments) != set(properties):
        return False
    paths = arguments.get("paths")
    if (
        not isinstance(paths, list) or not 1 <= len(paths) <= 64
        or any(
            not isinstance(path, str) or not path or len(path) > 512
            or Path(path).is_absolute() or "\\" in path or "\x00" in path
            or Path(path).as_posix() != path
            or any(part in {"", ".", ".."} for part in Path(path).parts)
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            for path in paths
        )
        or len(set(paths)) != len(paths)
    ):
        return False
    if name == "git_checkpoint":
        task_id = arguments.get("task_id")
        return (
            isinstance(task_id, str) and len(task_id) <= 128
            and type(arguments.get("require_complete")) is bool
        )
    checkpoint_id = arguments.get("checkpoint_id")
    return isinstance(checkpoint_id, str) and re.fullmatch(r"[a-f0-9]{32}", checkpoint_id) is not None


def _restore_result(
    success: bool,
    error_code: str | None,
    records: list[dict[str, Any]],
    limitation: str,
) -> dict[str, Any]:
    return {
        "ok": success,
        "success": success,
        "operation": "restore_checkpoint",
        "error_code": error_code,
        "records": records,
        "result_count": len(records),
        "truncated": False,
        "limitations": [limitation],
    }


def _tool_failure(
    operation: str,
    error_code: str,
    error: str,
    *,
    workspace: Path | None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "operation": operation,
        "repository": None,
        "workspace": str(workspace) if workspace is not None else None,
        "records": [],
        "result_count": 0,
        "truncated": False,
        "limitations": [],
        "error_code": error_code,
        "error": error[:512],
    }


class Tools:
    def __init__(
        self,
        sandbox: ExecutionBackend,
        approve: Approval,
        *,
        policy: AutonomyPolicy | None = None,
    ) -> None:
        self.sandbox, self.approve = sandbox, approve
        self.policy = policy or AutonomyPolicy()
        self._indexes: dict[Path, RepositoryIndex] = {}

    def _default_policy_request(
        self, name: str, arguments: dict[str, Any],
    ) -> PolicyRequest:
        workspace = ""
        if self.sandbox.workspace is not None:
            try:
                workspace = str(Path(self.sandbox.workspace).resolve(strict=True))
            except (OSError, ValueError):
                workspace = str(self.sandbox.workspace)
        return self.policy.create_request(
            name,
            arguments,
            mode=AutonomyMode.AGENT,
            task_id="dispatcher",
            step_id=None,
            backend_identity=self.sandbox.settings.execution_mode,
            workspace=workspace,
        )

    def _evaluate_task_policy(
        self,
        request: PolicyRequest,
        name: str,
        arguments: dict[str, Any],
        session: Session | None,
        cancellation: threading.Event | None,
    ) -> dict[str, Any] | None:
        try:
            request.validate()
            category = classify_operation(
                name,
                arguments,
                category_override=request.category,
            )
            arguments_match = (
                request.arguments_fingerprint == argument_fingerprint(arguments)
                and category == request.category
            )
            tool_matches = request.tool_name == name
            backend_matches = (
                request.backend_identity == self.sandbox.settings.execution_mode
            )
            workspace_matches = True
            if session is not None:
                workspace_matches = self.sandbox.matches(session)
            if request.task_id != "dispatcher":
                if session is None or not self.sandbox.matches(session):
                    workspace_matches = False
                if self.sandbox.workspace is None:
                    workspace_matches = False
                else:
                    try:
                        active_workspace = str(
                            Path(self.sandbox.workspace).resolve(strict=True),
                        )
                    except (OSError, ValueError):
                        active_workspace = ""
                    workspace_matches = (
                        workspace_matches and request.workspace == active_workspace
                    )
            effective_request = replace(
                request,
                tool_name=name if name in {
                    "read_file", "list_files", "write_file", "patch_file",
                    "delete_file", "terminal", "fetch_url", "get_project_structure",
                    "find_symbol", "find_definition", "find_references",
                    "find_callers", "find_implementations", "find_imports",
                    "find_tests", "search_code", "get_diagnostics", "git_status",
                    "git_diff", "git_log", "git_show", "git_checkpoint",
                    "restore_checkpoint",
                } else request.tool_name,
                backend_valid=request.backend_valid and backend_matches,
                workspace_valid=request.workspace_valid and workspace_matches,
                arguments_valid=(
                    request.arguments_valid and tool_matches and arguments_match
                ),
            )
            decision = self.policy.evaluate(
                effective_request,
                cancelled=bool(cancellation and cancellation.is_set()),
            )
        except Exception:
            _logger.exception("Autonomy policy evaluation failed; denying tool dispatch")
            return {
                "ok": False,
                "denied": True,
                "error_code": "POLICY_EVALUATION_ERROR",
                "error": "Policy evaluation failed; the operation was denied.",
                "policy_decision": PolicyDecisionType.DENY.value,
                "policy_reason": PolicyReason.POLICY_EVALUATION_ERROR.value,
            }
        if decision.decision == PolicyDecisionType.DENY:
            if decision.reason == PolicyReason.CANCELLED:
                if name == "restore_checkpoint":
                    return _restore_result(
                        False, "CANCELLED", [], decision.explanation,
                    )
                if name in GIT_ARGUMENTS:
                    return _tool_failure(
                        name, "CANCELLED", decision.explanation,
                        workspace=self.sandbox.workspace,
                    )
                if name == "git_checkpoint":
                    return _tool_failure(
                        name, "CANCELLED", decision.explanation,
                        workspace=self.sandbox.workspace,
                    )
            return {
                "ok": False,
                "denied": True,
                "error_code": "POLICY_DENIED",
                "error": decision.explanation,
                "policy_decision": decision.decision.value,
                "policy_reason": decision.reason.value,
            }
        return None

    async def call(
        self,
        name: str,
        arguments: Any,
        *,
        session: Session | None = None,
        approval_observer: ApprovalObserver | None = None,
        dispatch_guard: DispatchGuard | None = None,
        mutation_observer: MutationObserver | None = None,
        event_observer: ToolEventObserver | None = None,
        expected_preimage: tuple[bool, str | None] | None = None,
        cancellation: threading.Event | None = None,
        policy_request: PolicyRequest | None = None,
    ) -> dict[str, Any]:
        if (
            name not in PROPERTIES and name not in INTELLIGENCE_ARGUMENTS
            and name not in GIT_ARGUMENTS and name not in CHECKPOINT_ARGUMENTS
        ):
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "Unknown tool or invalid arguments"}
        if name in GIT_ARGUMENTS:
            if not validate_git_arguments(name, arguments):
                return {
                    "ok": False,
                    "error": f"{name} requires its exact bounded schema; revisions must be commit hashes and paths workspace-relative",
                    "error_code": "INVALID_ARGUMENTS",
                }
        elif name in CHECKPOINT_ARGUMENTS:
            if not validate_checkpoint_arguments(name, arguments):
                return {
                    "ok": False,
                    "error_code": "INVALID_ARGUMENTS",
                    "error": f"{name} requires its exact bounded workspace-relative schema",
                }
        elif name in INTELLIGENCE_ARGUMENTS:
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
        try:
            serialized_arguments = json.dumps(
                arguments, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            )
            if len(serialized_arguments.encode("utf-8")) > self.sandbox.settings.output_bytes * 2:
                return {"ok": False, "error": "Tool arguments exceed configured limit"}
            arguments = json.loads(serialized_arguments)
        except (TypeError, ValueError, OverflowError):
            return {
                "ok": False,
                "error_code": "INVALID_ARGUMENTS",
                "error": "Tool arguments could not be safely fingerprinted",
            }
        if cancellation is not None and cancellation.is_set():
            if name == "restore_checkpoint":
                return _restore_result(
                    False, "CANCELLED", [], "Restore was cancelled before policy evaluation.",
                )
            if name in GIT_ARGUMENTS or name == "git_checkpoint":
                return _tool_failure(
                    name, "CANCELLED", "Operation was cancelled before policy evaluation.",
                    workspace=self.sandbox.workspace,
                )
            return {
                "ok": False,
                "denied": True,
                "error_code": "CANCELLED",
                "error": "Operation was cancelled before policy evaluation.",
            }
        if len(serialized_arguments.encode("utf-8")) > self.sandbox.settings.output_bytes * 2:
            return {"ok": False, "error": "Tool arguments exceed configured limit"}
        if name in GIT_ARGUMENTS and (
            session is None or not self.sandbox.matches(session)
        ):
            return {
                "ok": False,
                "error": "Git inspection requires the matching active conversation workspace",
                "error_code": "WORKSPACE_CHANGED",
            }
        if name in CHECKPOINT_ARGUMENTS and (
            session is None or not self.sandbox.matches(session)
        ):
            return {
                "ok": False,
                "error": "Checkpoint operations require the matching active conversation workspace",
                "error_code": "CHECKPOINT_SCOPE_VIOLATION",
            }
        if policy_request is None:
            policy_request = self._default_policy_request(name, arguments)
        policy_denial = self._evaluate_task_policy(
            policy_request, name, arguments, session, cancellation,
        )
        if policy_denial is not None:
            return policy_denial
        if name in INTELLIGENCE_TOOLS:
            if session is None or not self.sandbox.matches(session):
                return {"ok": False, "error": "Repository intelligence requires the matching active conversation workspace"}
            policy_denial = self._evaluate_task_policy(
                policy_request, name, arguments, session, cancellation,
            )
            if policy_denial is not None:
                return policy_denial
            if (
                cancellation is not None and cancellation.is_set()
                or dispatch_guard is not None and not await dispatch_guard()
            ):
                return {"ok": False, "denied": True, "error_code": "CANCELLED", "error": "Action cancelled before dispatch"}
            return await self._intelligence(name, arguments)
        prepared_restore: PreparedRestore | None = None
        checkpoint_manager: CheckpointManager | None = None
        if name == "restore_checkpoint":
            try:
                assert session is not None
                checkpoint_manager = CheckpointManager(self.sandbox.settings)
                if cancellation is not None and cancellation.is_set():
                    return _restore_result(
                        False, "CANCELLED", [], "Restore was cancelled before preflight.",
                    )
                policy_denial = self._evaluate_task_policy(
                    policy_request, name, arguments, session, cancellation,
                )
                if policy_denial is not None:
                    return policy_denial
                if dispatch_guard is not None and not await dispatch_guard():
                    return _restore_result(
                        False, "CANCELLED", [], "Restore was cancelled before preflight.",
                    )
                prepared = await self._restore_preflight(
                    checkpoint_manager,
                    session,
                    self._repository_index(session),
                    arguments["checkpoint_id"],
                    arguments["paths"],
                    cancellation=cancellation,
                )
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "success": False,
                    "operation": name,
                    "error_code": "CHECKPOINT_SCOPE_VIOLATION",
                    "error": str(exc)[:512],
                }
            if isinstance(prepared, dict):
                await self._observe_event(
                    event_observer,
                    "restoration_conflict" if prepared.get("error_code") == "CHECKPOINT_CONFLICT"
                    else "restoration_interrupted",
                    str(prepared.get("error", ""))[:512],
                )
                return prepared
            if cancellation is not None and cancellation.is_set():
                return _restore_result(
                    False, "CANCELLED", [], "Restore was cancelled after preflight.",
                )
            if dispatch_guard is not None and not await dispatch_guard():
                return _restore_result(
                    False, "CANCELLED", [], "Restore was cancelled after preflight.",
                )
            prepared_restore = prepared
        digest: str | None = None
        description = json.dumps(arguments, ensure_ascii=True, indent=2)
        try:
            if name in {"write_file", "patch_file", "delete_file"}:
                preview = await self.sandbox.execute("preview", {"path": arguments["path"]})
                if not preview.get("ok"):
                    return preview
                digest = preview["sha256"]
                if expected_preimage is not None and (
                    (digest is not None) != expected_preimage[0]
                    or digest != expected_preimage[1]
                ):
                    return {
                        "ok": False,
                        "error_code": "CHECKPOINT_CONFLICT",
                        "error": "File changed after checkpoint restoration preflight",
                    }
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
                if name in GIT_ARGUMENTS:
                    await self._observe_event(event_observer, "git_operation_started", name)
                    description = (
                        f"{GIT_DESCRIPTIONS[name]}\n\n"
                        f"Workspace: {self.sandbox.workspace}\n"
                        f"Validated request: {json.dumps(arguments, ensure_ascii=True)}"
                    )
                elif name in CHECKPOINT_ARGUMENTS:
                    description = (
                        f"{CHECKPOINT_DESCRIPTIONS[name]}\n\n"
                        f"Workspace: {self.sandbox.workspace}\n"
                        f"Validated request: {json.dumps(arguments, ensure_ascii=True)}"
                    )
                    event_kind = (
                        "checkpoint_requested" if name == "git_checkpoint"
                        else "restoration_approval_required"
                    )
                    await self._observe_event(event_observer, event_kind, name)
                if self.sandbox.settings.execution_mode == "host":
                    description = f"HOST EXECUTION // NOT ISOLATED\nWorkspace: {self.sandbox.workspace}\n\n{description}"
                policy_denial = self._evaluate_task_policy(
                    policy_request, name, arguments, session, cancellation,
                )
                if policy_denial is not None:
                    return policy_denial
                if dispatch_guard is not None and not await dispatch_guard():
                    return {
                        "ok": False,
                        "denied": True,
                        "error_code": "CANCELLED",
                        "error": "Action eligibility changed before approval.",
                    }
                if approval_observer is not None:
                    await approval_observer(name, description, None)
                if name == "restore_checkpoint":
                    if cancellation is not None and cancellation.is_set():
                        return _restore_result(
                            False, "CANCELLED", [], "Restore was cancelled before approval.",
                        )
                    if dispatch_guard is not None and not await dispatch_guard():
                        return _restore_result(
                            False, "CANCELLED", [], "Restore was cancelled before approval.",
                        )
                    if cancellation is not None and cancellation.is_set():
                        return _restore_result(
                            False, "CANCELLED", [], "Restore was cancelled before approval.",
                        )
                approved = await self.approve(name, description)
                if type(approved) is not bool:
                    _logger.error(
                        "Approval callback returned a malformed result for %s; denying dispatch",
                        name,
                    )
                    approved = False
                if approval_observer is not None:
                    await approval_observer(name, description, approved)
                if not approved:
                    if name in GIT_ARGUMENTS:
                        return _tool_failure(
                            name, "APPROVAL_DENIED", "User denied Git inspection",
                            workspace=self.sandbox.workspace,
                        )
                    if name == "git_checkpoint":
                        await self._observe_event(
                            event_observer, "checkpoint_rejected", "approval denied",
                        )
                        return _tool_failure(
                            name, "CHECKPOINT_APPROVAL_DENIED",
                            "User denied checkpoint creation",
                            workspace=self.sandbox.workspace,
                        )
                    if name == "restore_checkpoint":
                        return {
                            "ok": False,
                            "success": False,
                            "denied": True,
                            "error_code": "RESTORE_APPROVAL_DENIED",
                            "error": "User denied checkpoint restoration",
                        }
                    return {"ok": False, "denied": True, "error": "User denied this action"}
                policy_denial = self._evaluate_task_policy(
                    policy_request, name, arguments, session, cancellation,
                )
                if policy_denial is not None:
                    return policy_denial
                if dispatch_guard is not None and not await dispatch_guard():
                    if name in GIT_ARGUMENTS:
                        return _tool_failure(
                            name, "CANCELLED", "Git inspection cancelled before dispatch",
                            workspace=self.sandbox.workspace,
                        )
                    if name == "git_checkpoint":
                        await self._observe_event(
                            event_observer, "checkpoint_rejected", "cancelled before dispatch",
                        )
                        return _tool_failure(
                            name, "CANCELLED", "Checkpoint creation cancelled before dispatch",
                            workspace=self.sandbox.workspace,
                        )
                    if name == "restore_checkpoint":
                        await self._observe_event(
                            event_observer, "restoration_interrupted", "cancelled before dispatch",
                        )
                        return _restore_result(
                            False, "CANCELLED", [], prepared_restore.limitation
                            if prepared_restore else "Restore preflight was not retained.",
                        )
                    return {"ok": False, "denied": True, "error": "Action cancelled before dispatch"}
                policy_denial = self._evaluate_task_policy(
                    policy_request, name, arguments, session, cancellation,
                )
                if policy_denial is not None:
                    return policy_denial
            policy_denial = self._evaluate_task_policy(
                policy_request, name, arguments, session, cancellation,
            )
            if policy_denial is not None:
                return policy_denial
            if dispatch_guard is not None and not await dispatch_guard():
                return {
                    "ok": False,
                    "denied": True,
                    "error_code": "CANCELLED",
                    "error": "Action eligibility changed before dispatch.",
                }
            if name in GIT_ARGUMENTS:
                result = await GitInspector(self.sandbox).inspect(name, arguments, session)
                await self._observe_event(
                    event_observer,
                    "git_operation_completed",
                    f"{name}: {'succeeded' if result.get('success') else 'failed'}",
                )
                return result
            if name in CHECKPOINT_ARGUMENTS:
                assert session is not None
                checkpoint_manager = checkpoint_manager or CheckpointManager(self.sandbox.settings)
                if name == "git_checkpoint":
                    try:
                        result = await asyncio.to_thread(
                            checkpoint_manager.create,
                            session,
                            self._repository_index(session),
                            arguments["paths"],
                            task_id=arguments["task_id"] or None,
                            require_complete=arguments["require_complete"],
                            cancellation=cancellation,
                        )
                    except (OSError, ValueError, InterruptedError) as exc:
                        result = _tool_failure(
                            name,
                            "CHECKPOINT_SCOPE_VIOLATION",
                            str(exc),
                            workspace=self.sandbox.workspace,
                        )
                    await self._observe_event(
                        event_observer,
                        "checkpoint_created" if result.get("ok") else "checkpoint_rejected",
                        str(result.get("checkpoint_id") or result.get("error") or "")[:512],
                    )
                    return result
                assert prepared_restore is not None
                result = await self._restore_checkpoint(
                    checkpoint_manager,
                    prepared_restore,
                    session,
                    dispatch_guard,
                    event_observer,
                    cancellation,
                )
                return result
            if name in {"write_file", "patch_file", "delete_file"} and mutation_observer is not None:
                try:
                    await mutation_observer("before", name, arguments, None)
                except (OSError, ValueError) as exc:
                    return {
                        "ok": False,
                        "error_code": "CHANGE_BASELINE_UNAVAILABLE",
                        "error": str(exc)[:512],
                    }
                return await self._execute_mutation(
                    name, arguments, digest, mutation_observer,
                )
            return await self.sandbox.execute(name, arguments, digest)
        except TimeoutError:
            return {"ok": False, "error": f"{self.sandbox.settings.execution_mode.title()} tool timed out"}
        except (SandboxError, OSError) as exc:
            return {"ok": False, "error": f"{self.sandbox.settings.execution_mode.title()} tool failed: {exc}"}

    def _repository_index(self, session: Session) -> RepositoryIndex:
        workspace = self.sandbox.workspace
        if workspace is None or not self.sandbox.matches(session):
            raise ValueError("Checkpoint workspace/backend identity changed")
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
        return index

    async def _restore_checkpoint(
        self,
        manager: CheckpointManager,
        prepared: PreparedRestore,
        session: Session,
        dispatch_guard: DispatchGuard | None,
        event_observer: ToolEventObserver | None,
        cancellation: threading.Event | None,
    ) -> dict[str, Any]:
        if cancellation is not None and cancellation.is_set():
            return _restore_result(
                False, "CANCELLED", [], "Restore was cancelled before post-approval validation.",
            )
        if dispatch_guard is not None and not await dispatch_guard():
            return _restore_result(
                False, "CANCELLED", [], "Restore was cancelled before post-approval validation.",
            )
        try:
            repository = self._repository_index(session)
            refreshed = await self._restore_preflight(
                manager,
                session,
                repository,
                prepared.checkpoint_id,
                [item.path for item in prepared.files],
                prepared=prepared,
                cancellation=cancellation,
            )
        except (OSError, ValueError) as exc:
            return _restore_result(
                False, "CHECKPOINT_SCOPE_VIOLATION", [], str(exc)[:512],
            )
        if isinstance(refreshed, dict):
            await self._observe_event(
                event_observer,
                "restoration_conflict" if refreshed.get("error_code") == "CHECKPOINT_CONFLICT"
                else "restoration_interrupted",
                str(refreshed.get("error", ""))[:512],
            )
            return refreshed
        prepared = refreshed
        if cancellation is not None and cancellation.is_set():
            return _restore_result(
                False, "CANCELLED", [], "Restore was cancelled after post-approval validation.",
            )
        if dispatch_guard is not None and not await dispatch_guard():
            return _restore_result(
                False, "CANCELLED", [], "Restore was cancelled after post-approval validation.",
            )
        records: list[dict[str, Any]] = []
        for item in prepared.files:
            if dispatch_guard is not None and not await dispatch_guard():
                outcome = "interrupted"
                records.append({"path": item.path, "outcome": outcome})
                await self._observe_event(event_observer, "restoration_interrupted", item.path)
                return _restore_result(
                    False, "CANCELLED" if not records[:-1] else "RESTORE_PARTIAL_FAILURE",
                    records, prepared.limitation,
                )
            if (
                item.expected_exists == item.original_exists
                and item.expected_sha256 == item.restore_sha256
            ):
                records.append({"path": item.path, "outcome": "unchanged"})
                continue
            if item.original_exists:
                assert item.original_content is not None
                name = "write_file"
                arguments: dict[str, Any] = {
                    "path": item.path,
                    "content": item.original_content,
                }
            else:
                name = "delete_file"
                arguments = {"path": item.path}
            result = await self.call(
                name,
                arguments,
                session=session,
                dispatch_guard=dispatch_guard,
                expected_preimage=(item.expected_exists, item.expected_sha256),
            )
            if result.get("ok") is True:
                try:
                    await asyncio.to_thread(
                        manager.mark_restored,
                        prepared.checkpoint_id,
                        item.path,
                    )
                except (OSError, ValueError) as exc:
                    records.append({
                        "path": item.path,
                        "outcome": "uncertain",
                        "error": str(exc)[:256],
                    })
                    return _restore_result(
                        False, "RESTORE_PARTIAL_FAILURE", records, prepared.limitation,
                    )
                records.append({"path": item.path, "outcome": "restored"})
            else:
                outcome = "denied" if result.get("denied") is True else "failed"
                records.append({
                    "path": item.path,
                    "outcome": outcome,
                    "error_code": result.get("error_code"),
                })
                code = (
                    "RESTORE_APPROVAL_DENIED" if outcome == "denied"
                    else "CHECKPOINT_CONFLICT" if result.get("error_code") == "CHECKPOINT_CONFLICT"
                    else "RESTORE_PARTIAL_FAILURE"
                )
                await self._observe_event(
                    event_observer,
                    "restoration_conflict" if code == "CHECKPOINT_CONFLICT" else "restoration_interrupted",
                    item.path,
                )
                return _restore_result(False, code, records, prepared.limitation)
        await self._observe_event(
            event_observer, "restoration_completed", prepared.checkpoint_id,
        )
        return _restore_result(True, None, records, prepared.limitation)

    async def _restore_preflight(
        self,
        manager: CheckpointManager,
        session: Session,
        repository: RepositoryIndex,
        checkpoint_id: str,
        paths: list[str],
        *,
        prepared: PreparedRestore | None = None,
        cancellation: threading.Event | None = None,
    ) -> PreparedRestore | dict[str, Any]:
        worker_cancellation = cancellation or threading.Event()
        deadline = time.monotonic() + min(10.0, max(0.1, self.sandbox.settings.command_timeout))
        if prepared is None:
            worker = asyncio.create_task(asyncio.to_thread(
                manager.prepare_restore,
                session,
                repository,
                checkpoint_id,
                paths,
                cancellation=worker_cancellation,
                deadline=deadline,
            ))
        else:
            worker = asyncio.create_task(asyncio.to_thread(
                manager.revalidate_restore,
                session,
                repository,
                prepared,
                cancellation=worker_cancellation,
                deadline=deadline,
            ))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except asyncio.CancelledError:
            worker_cancellation.set()
            worker.add_done_callback(_consume_worker_result)
            raise
        except TimeoutError:
            worker_cancellation.set()
            worker.add_done_callback(_consume_worker_result)
            return {
                "ok": False,
                "success": False,
                "operation": "restore_checkpoint",
                "error_code": "RESTORE_TIMEOUT",
                "error": "Checkpoint restoration preflight exceeded its time limit.",
                "records": [],
                "result_count": 0,
                "truncated": False,
                "limitations": [],
            }
        except Exception as exc:
            return {
                "ok": False,
                "success": False,
                "operation": "restore_checkpoint",
                "error_code": "CHECKPOINT_PREFLIGHT_FAILED",
                "error": str(exc)[:512],
                "records": [],
                "result_count": 0,
                "truncated": False,
                "limitations": [],
            }
        if worker_cancellation.is_set():
            return {
                "ok": False,
                "success": False,
                "operation": "restore_checkpoint",
                "error_code": "CANCELLED",
                "error": "Checkpoint restoration preflight was cancelled.",
                "records": [],
                "result_count": 0,
                "truncated": False,
                "limitations": [],
            }
        return result

    @staticmethod
    async def _observe_event(
        callback: ToolEventObserver | None,
        kind: str,
        message: str | None,
    ) -> None:
        if callback is not None:
            try:
                await callback(kind, message[:512] if message else None)
            except Exception as exc:
                _logger.warning(
                    "Tool observer delivery failed for event %s: %s",
                    kind, str(exc)[:512],
                )

    async def _execute_mutation(
        self,
        name: str,
        arguments: dict[str, Any],
        digest: str | None,
        observer: MutationObserver,
    ) -> dict[str, Any]:
        try:
            try:
                result = await self.sandbox.execute(name, arguments, digest)
            except TimeoutError:
                result = {
                    "ok": False,
                    "error_code": "TIMEOUT",
                    "error": f"{self.sandbox.settings.execution_mode.title()} tool timed out",
                }
            except (SandboxError, OSError) as exc:
                result = {
                    "ok": False,
                    "error_code": "TOOL_ERROR",
                    "error": f"{self.sandbox.settings.execution_mode.title()} tool failed: {exc}",
                }
        except asyncio.CancelledError:
            try:
                await asyncio.shield(observer("after", name, arguments, None))
            except (OSError, ValueError):
                pass
            raise
        try:
            evidence = await observer("after", name, arguments, result)
        except (OSError, ValueError) as exc:
            result = dict(result)
            result.update({
                "mutation_changed": False,
                "change_attribution_complete": False,
                "attribution_error": str(exc)[:512],
            })
        else:
            if evidence is not None:
                result = dict(result)
                result.update(evidence)
        return result

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
