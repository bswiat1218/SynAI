from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from synai.execution_backend import ExecutionBackend, validate_workspace
from synai.models import Session


GIT_ARGUMENTS: dict[str, dict[str, dict[str, Any]]] = {
    "git_status": {},
    "git_diff": {
        "mode": {
            "type": "string",
            "enum": ["unstaged", "staged", "revision"],
            "description": "Which changes to inspect",
        },
        "revision": {
            "type": "string",
            "description": "Full or abbreviated hexadecimal commit hash; required only for revision mode",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string", "maxLength": 512},
            "description": "Optional workspace-relative paths",
            "maxItems": 64,
        },
        "include_patch": {
            "type": "boolean",
            "description": "Include a bounded textual patch",
        },
    },
    "git_log": {
        "limit": {
            "type": "integer",
            "description": "Maximum number of recent commits",
            "minimum": 1,
            "maximum": 100,
        },
    },
    "git_show": {
        "revision": {
            "type": "string",
            "description": "Full or abbreviated hexadecimal commit hash",
            "maxLength": 40,
        },
        "path": {
            "type": "string",
            "description": "Optional workspace-relative path",
            "maxLength": 512,
        },
    },
}

GIT_DESCRIPTIONS = {
    "git_status": "Inspect bounded Git worktree status. Requires approval.",
    "git_diff": "Inspect bounded Git changes without changing the index. Requires approval.",
    "git_log": "Inspect bounded recent Git history. Requires approval.",
    "git_show": "Inspect one validated commit and optional workspace path. Requires approval.",
}
GIT_TOOLS = frozenset(GIT_ARGUMENTS)

_REVISION = re.compile(r"[0-9a-fA-F]{7,40}\Z")
_HEX = re.compile(r"[0-9a-fA-F]{40}\Z")
_OUTPUT_LIMIT = 256 * 1024
_PATCH_LIMIT = 64 * 1024
_MAX_RECORDS = 256


@dataclass(frozen=True)
class RepositoryCapability:
    available: bool
    repository_root: str | None
    git_directory: str | None
    workspace_root: str
    execution_workspace_root: str
    error_code: str | None = None
    limitation: str | None = None
    session: Session | None = None

    def identity(self) -> dict[str, str | None]:
        return {
            "root": self.repository_root,
            "git_directory": self.git_directory,
        }


class GitInspector:
    """Bounded read-only Git inspection through the configured tool backend."""

    def __init__(self, backend: ExecutionBackend) -> None:
        self.backend = backend

    def _assert_backend_current(
        self,
        session: Session,
        workspace: Path,
        execution_workspace: Path,
    ) -> None:
        expected_execution_workspace = (
            Path("/workspace") if self.backend.settings.execution_mode == "sandbox"
            else workspace
        )
        if (
            self.backend.workspace != workspace
            or self.backend.execution_workspace != execution_workspace
            or execution_workspace != expected_execution_workspace
            or not self.backend.matches(session)
        ):
            raise _GitError(
                "WORKSPACE_CHANGED",
                "Workspace or execution backend identity changed during Git inspection.",
            )
        try:
            validated = validate_workspace(
                workspace,
                self.backend.settings,
                sandbox=self.backend.settings.execution_mode == "sandbox",
            )
        except (OSError, ValueError) as exc:
            raise _GitError(
                "WORKSPACE_CHANGED", f"Workspace is unavailable: {exc}",
            ) from exc
        if validated != workspace:
            raise _GitError("WORKSPACE_CHANGED", "Validated workspace identity changed.")

    async def inspect(
        self,
        operation: str,
        arguments: dict[str, Any],
        session: Session | None,
    ) -> dict[str, Any]:
        workspace_identity = str(self.backend.workspace) if self.backend.workspace else None
        capability: RepositoryCapability | None = None
        try:
            capability = await self.discover(session)
            if not capability.available:
                return self._result(
                    operation, capability, workspace_identity, False, [],
                    capability.error_code or "NOT_GIT_REPOSITORY",
                    capability.limitation or "Git inspection is unavailable.",
                    outcome=(
                        "UNAVAILABLE" if capability.error_code in {
                            "NOT_GIT_REPOSITORY", "GIT_UNAVAILABLE",
                        } else "FAILED"
                    ),
                )
            handlers = {
                "git_status": self._status,
                "git_diff": self._diff,
                "git_log": self._log,
                "git_show": self._show,
            }
            records, truncated, limitations, exit_code = await handlers[operation](
                capability, arguments,
            )
            return self._result(
                operation, capability, workspace_identity, not truncated, records,
                None, None, truncated=truncated, limitations=limitations,
                outcome="PARTIAL" if truncated else "COMPLETE",
                exit_code=exit_code,
            )
        except _GitError as exc:
            return self._result(
                operation, capability, workspace_identity, False, [],
                exc.code, str(exc), outcome=exc.outcome, exit_code=exc.exit_code,
                truncated=exc.output_truncated,
            )
        except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as exc:
            code = "GIT_UNAVAILABLE" if isinstance(exc, (OSError, TimeoutError)) else "GIT_OPERATION_FAILED"
            return self._result(
                operation, capability, workspace_identity, False, [],
                code, str(exc)[:512],
                outcome="TIMEOUT" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "FAILED",
            )

    async def discover(self, session: Session | None) -> RepositoryCapability:
        backend = self.backend
        workspace = backend.workspace
        execution_workspace = backend.execution_workspace
        if (
            session is None or workspace is None or execution_workspace is None
            or not backend.matches(session)
        ):
            raise _GitError("WORKSPACE_CHANGED", "Git inspection requires the matching active workspace.")
        try:
            root = validate_workspace(
                workspace,
                backend.settings,
                sandbox=backend.settings.execution_mode == "sandbox",
            )
        except (OSError, ValueError) as exc:
            raise _GitError("WORKSPACE_CHANGED", f"Workspace is unavailable: {exc}") from exc
        if root != workspace.resolve(strict=True):
            raise _GitError("WORKSPACE_CHANGED", "Validated workspace identity changed.")
        sandbox = backend.settings.execution_mode == "sandbox"
        expected_execution_workspace = Path("/workspace") if sandbox else root
        if execution_workspace != expected_execution_workspace:
            raise _GitError(
                "WORKSPACE_CHANGED",
                "Execution backend workspace mapping does not match the selected workspace.",
            )
        prefix = await self._run(
            "rev-parse --show-toplevel",
            allow_failure=True,
            session=session,
            workspace=root,
            execution_workspace=execution_workspace,
        )
        if prefix["exit_code"] != 0:
            if prefix["exit_code"] == 127:
                raise _GitError(
                    "GIT_UNAVAILABLE", "The Git executable is unavailable.",
                    outcome="UNAVAILABLE", exit_code=127,
                )
            diagnostic = prefix["stderr"].lower()
            if "not a git repository" not in diagnostic and "cannot change to" not in diagnostic:
                raise _GitError(
                    "GIT_OPERATION_FAILED",
                    f"Git repository metadata is malformed or inaccessible: {prefix['stderr'][:384]}",
                )
            return RepositoryCapability(
                False, None, None, str(root), str(execution_workspace), "NOT_GIT_REPOSITORY",
                "The selected workspace is not inside a discoverable Git repository.", session,
            )
        repo_root = self._workspace_relative_git_path(
            prefix["stdout"].strip(), root, execution_workspace,
            sandbox=sandbox, strict=True,
        )
        if repo_root is None:
            return RepositoryCapability(
                False, None, None, str(root), str(execution_workspace), "UNSAFE_PATH",
                "The repository root is outside the selected workspace boundary.", session,
            )
        gitdir_result = await self._run(
            "rev-parse --absolute-git-dir",
            allow_failure=True,
            session=session,
            workspace=root,
            execution_workspace=execution_workspace,
        )
        if gitdir_result["exit_code"] != 0:
            return RepositoryCapability(
                False, repo_root, None, str(root), str(execution_workspace), "GIT_OPERATION_FAILED",
                "Git metadata location could not be resolved.", session,
            )
        gitdir = self._workspace_relative_git_path(
            gitdir_result["stdout"].strip(), root, execution_workspace,
            sandbox=sandbox, strict=True,
        )
        if gitdir is None:
            return RepositoryCapability(
                False, repo_root, None, str(root), str(execution_workspace), "UNSAFE_PATH",
                "Git metadata is outside the selected workspace boundary.", session,
            )
        return RepositoryCapability(
            True, repo_root, gitdir, str(root), str(execution_workspace),
            session=session,
        )

    @staticmethod
    def _workspace_relative_git_path(
        reported: str,
        workspace: Path,
        execution_workspace: Path,
        *,
        sandbox: bool,
        strict: bool,
    ) -> str | None:
        path = Path(reported)
        if not path.is_absolute() or ".." in path.parts:
            raise _GitError("GIT_OPERATION_FAILED", "Git returned an invalid absolute workspace path.")
        if sandbox:
            if reported != path.as_posix():
                raise _GitError("GIT_OPERATION_FAILED", "Git returned a non-normalized sandbox path.")
            if not path.is_relative_to(execution_workspace):
                return None
            relative = path.relative_to(execution_workspace)
            host_path = workspace / relative
        else:
            try:
                resolved = path.resolve(strict=strict)
            except (OSError, RuntimeError) as exc:
                raise _GitError("GIT_OPERATION_FAILED", "Git returned an invalid repository path.") from exc
            if not resolved.is_relative_to(workspace):
                return None
            relative = resolved.relative_to(workspace)
            host_path = resolved
        try:
            resolved_host_path = host_path.resolve(strict=strict)
        except (OSError, RuntimeError) as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git returned an inaccessible workspace path.") from exc
        if not resolved_host_path.is_relative_to(workspace):
            return None
        return relative.as_posix() or "."

    async def _status(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str], int]:
        del arguments
        output = await self._git(
            capability,
            "status --porcelain=v2 -z --untracked-files=all --renames",
        )
        if output["lossy"]:
            return [], True, ["Git status contained text decoded without lossless filename bytes."], output["exit_code"]
        records, malformed = _parse_status(
            output["stdout_bytes"],
            capability,
        )
        truncated = (
            output["truncated"] or output["outcome"] != "COMPLETE"
            or len(records) > _MAX_RECORDS or malformed
        )
        limitations = ["Status output was bounded, lossy, or contained an incomplete record."] if truncated else []
        return records[:_MAX_RECORDS], truncated, limitations, output["exit_code"]

    async def _diff(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str], int]:
        paths = _repo_paths(capability, arguments["paths"])
        mode = arguments["mode"]
        if mode == "revision":
            revision = await self._resolve_revision(capability, arguments["revision"])
            selector = f"{revision} --"
        elif mode == "staged":
            selector = "--cached --"
        else:
            selector = "--"
        suffix = " " + " ".join(shlex.quote(path) for path in paths) if paths else ""
        name_result = await self._git(
            capability, f"diff --name-status -z {selector}{suffix}",
        )
        numstat_result = await self._git(
            capability, f"diff --numstat -z {selector}{suffix}",
        )
        if name_result["lossy"] or numstat_result["lossy"]:
            return [], True, ["Git diff paths were decoded without lossless filename bytes."], (
                name_result["exit_code"] if name_result["lossy"] else numstat_result["exit_code"]
            )
        changes, malformed_names = _parse_name_status(name_result["stdout_bytes"])
        counts, malformed_counts = _parse_numstat(numstat_result["stdout_bytes"])
        records = []
        for change in changes[:_MAX_RECORDS]:
            repo_identity = change.get("destination_path") or change["source_path"]
            counts_record = counts.get(repo_identity, {})
            source_path = _workspace_path_from_repo(capability, change["source_path"])
            destination_path = (
                _workspace_path_from_repo(capability, change["destination_path"])
                if change.get("destination_path") else None
            )
            records.append({
                "source_path": source_path,
                "destination_path": destination_path,
                "category": change["category"],
                "additions": counts_record.get("additions"),
                "deletions": counts_record.get("deletions"),
                "binary": counts_record.get("binary", False),
            })
        truncated = (
            name_result["truncated"] or numstat_result["truncated"]
            or name_result["outcome"] != "COMPLETE"
            or numstat_result["outcome"] != "COMPLETE"
        )
        exit_code = (
            name_result["exit_code"] if name_result["outcome"] != "COMPLETE"
            else numstat_result["exit_code"]
        )
        if len(changes) > _MAX_RECORDS or malformed_names or malformed_counts:
            truncated = True
        limitations: list[str] = []
        if truncated:
            limitations.append("Change summary output was bounded or incomplete.")
        if arguments["include_patch"]:
            deadline = time.monotonic() + self.backend.settings.command_timeout
            for record in records[:16]:
                path = record["destination_path"] or record["source_path"]
                repo_path = _repo_path_from_workspace(capability, path)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    truncated = True
                    limitations.append("Patch collection reached the Git operation time limit.")
                    break
                patch = await asyncio.wait_for(
                    self._git(
                        capability,
                        f"diff --no-ext-diff --no-textconv --binary --no-color "
                        f"{selector}{' ' + shlex.quote(repo_path)}",
                        output_limit=_PATCH_LIMIT,
                    ),
                    timeout=remaining,
                )
                exit_code = patch["exit_code"]
                record["patch"] = (
                    None if record["binary"] else patch["stdout"][:_PATCH_LIMIT]
                )
                truncated = truncated or patch["truncated"] or patch["outcome"] != "COMPLETE"
                if patch["truncated"] or patch["outcome"] != "COMPLETE":
                    record.setdefault("limitations", []).append(
                        "Patch content was truncated or the Git command did not complete."
                    )
                if record["binary"]:
                    record.setdefault("limitations", []).append(
                        "Binary patch content is omitted; bounded metadata is available."
                    )
            for record in records[16:]:
                record["patch"] = None
                record.setdefault("limitations", []).append(
                    "Patch omitted because the per-operation patch-file bound was reached."
                )
        else:
            for record in records:
                record["patch"] = None
        return records, truncated, limitations, exit_code

    async def _log(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str], int]:
        limit = arguments["limit"]
        pretty = "--format=%x1e%H%x00%P%x00%ct%x00%an%x00%ae%x00%s%x00"
        output = await self._git(
            capability, f"log -n {limit} {pretty}", output_limit=_OUTPUT_LIMIT,
        )
        records, malformed = _parse_log(output["stdout_bytes"])
        truncated = (
            output["truncated"] or output["outcome"] != "COMPLETE"
            or output["lossy"] or malformed
        )
        return records, truncated, (
            ["Commit history output was bounded or incomplete."] if truncated else []
        ), output["exit_code"]

    async def _show(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str], int]:
        revision = await self._resolve_revision(capability, arguments["revision"])
        repo_paths = _repo_paths(
            capability, [arguments["path"]] if arguments["path"] else [],
        )
        suffix = " " + " ".join(shlex.quote(path) for path in repo_paths) if repo_paths else ""
        output = await self._git(
            capability,
            f"show --no-ext-diff --no-textconv --no-color --format=fuller {revision} --{suffix}",
            output_limit=_OUTPUT_LIMIT,
        )
        return [{
            "revision": revision,
            "path": arguments["path"] or None,
            "content": output["stdout"][:_OUTPUT_LIMIT],
            "content_truncated": (
                output["truncated"] or output["outcome"] != "COMPLETE" or output["lossy"]
            ),
        }], output["truncated"] or output["outcome"] != "COMPLETE" or output["lossy"], (
            ["Commit content was bounded and may be truncated."]
            if output["truncated"] or output["outcome"] != "COMPLETE" or output["lossy"] else []
        ), output["exit_code"]

    async def _resolve_revision(
        self, capability: RepositoryCapability, revision: str,
    ) -> str:
        if not _REVISION.fullmatch(revision):
            raise _GitError("INVALID_REVISION", "Revision must be a hexadecimal commit identifier.")
        result = await self._git(
            capability,
            f"rev-parse --verify --end-of-options {shlex.quote(revision)}^{{commit}}",
            allow_failure=True,
        )
        resolved = result["stdout"].strip().lower()
        if result["exit_code"] != 0 or result["outcome"] != "COMPLETE" or not _HEX.fullmatch(resolved):
            raise _GitError("INVALID_REVISION", "Revision does not resolve to a commit.")
        return resolved

    async def _git(
        self,
        capability: RepositoryCapability,
        arguments: str,
        *,
        output_limit: int = _OUTPUT_LIMIT,
        allow_failure: bool = False,
    ) -> dict[str, Any]:
        relative_cwd = capability.repository_root or "."
        command = (
            "env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null "
            "GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 "
            "GIT_PAGER=cat PAGER=cat GIT_OPTIONAL_LOCKS=0 "
            "GIT_EXTERNAL_DIFF= GIT_TRACE= GIT_TRACE_SETUP= "
            "git --no-pager --literal-pathspecs "
            "-c core.pager=cat -c core.fsmonitor=false -c core.untrackedCache=false "
            "-c diff.external= "
            f"-C {shlex.quote(relative_cwd)} {arguments}"
        )
        if capability.session is None:
            raise _GitError("WORKSPACE_CHANGED", "Git repository capability is not session-bound.")
        self._assert_backend_current(
            capability.session,
            Path(capability.workspace_root),
            Path(capability.execution_workspace_root),
        )
        try:
            result = await asyncio.wait_for(
                self.backend.execute("terminal", {"command": command, "cwd": "."}),
                timeout=self.backend.settings.command_timeout,
            )
        except TimeoutError as exc:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git operation exceeded the configured timeout.",
                outcome="TIMEOUT",
            ) from exc
        self._assert_backend_current(
            capability.session,
            Path(capability.workspace_root),
            Path(capability.execution_workspace_root),
        )
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise _GitError("GIT_OPERATION_FAILED", "Execution backend returned an invalid Git result.")
        if result.get("timed_out") is True:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git operation exceeded the backend timeout.",
                outcome="TIMEOUT",
                exit_code=result.get("exit_code") if type(result.get("exit_code")) is int else None,
                output_truncated=result.get("truncated") is True,
            )
        if result.get("cancelled") is True:
            raise _GitError(
                "CANCELLED", "Git operation was cancelled.", outcome="CANCELLED",
                exit_code=result.get("exit_code") if type(result.get("exit_code")) is int else None,
                output_truncated=result.get("truncated") is True,
            )
        exit_code = result.get("exit_code")
        if type(exit_code) is not int:
            raise _GitError("GIT_OPERATION_FAILED", "Git operation omitted its exit status.")
        stdout = result.get("stdout")
        if not isinstance(stdout, str):
            raise _GitError("GIT_OPERATION_FAILED", "Git operation returned invalid output.")
        try:
            raw_stdout, lossy = _lossless_output(result, "stdout", stdout)
        except (ValueError, binascii.Error) as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git operation returned invalid encoded output.") from exc
        backend_truncated = result.get("truncated") is True
        terminated_by_limit = result.get("terminated_by_output_limit") is True
        truncated = (
            backend_truncated
            or terminated_by_limit
            or len(raw_stdout) > output_limit
        )
        raw_stdout = raw_stdout[:output_limit]
        displayed_stdout = raw_stdout.decode("utf-8", errors="surrogateescape")
        if terminated_by_limit:
            outcome = "PARTIAL"
        elif exit_code != 0:
            if not allow_failure:
                message = result.get("stderr")
                detail = message[:384] if isinstance(message, str) else "Git command failed."
                raise _GitError(
                    "GIT_OPERATION_FAILED", detail or "Git command failed.",
                    exit_code=exit_code,
                    output_truncated=truncated,
                )
            outcome = "FAILED"
        elif result.get("ok") is not True:
            if not truncated:
                raise _GitError("GIT_OPERATION_FAILED", "Git command failed without an exit status explanation.")
            outcome = "PARTIAL"
        else:
            outcome = "PARTIAL" if truncated else "COMPLETE"
        return {
            "stdout": displayed_stdout,
            "stdout_bytes": raw_stdout,
            "exit_code": exit_code,
            "truncated": truncated,
            "lossy": lossy,
            "outcome": outcome,
        }

    async def _run(
        self,
        arguments: str,
        *,
        allow_failure: bool = False,
        session: Session,
        workspace: Path,
        execution_workspace: Path,
    ) -> dict[str, Any]:
        command = (
            "env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null "
            "GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 "
            "GIT_PAGER=cat PAGER=cat GIT_OPTIONAL_LOCKS=0 "
            "GIT_EXTERNAL_DIFF= GIT_TRACE= GIT_TRACE_SETUP= "
            "git --no-pager --literal-pathspecs "
            "-c core.pager=cat -c core.fsmonitor=false -c core.untrackedCache=false "
            "-c diff.external= "
            f"{arguments}"
        )
        self._assert_backend_current(session, workspace, execution_workspace)
        try:
            result = await asyncio.wait_for(
                self.backend.execute("terminal", {"command": command, "cwd": "."}),
                timeout=self.backend.settings.command_timeout,
            )
        except TimeoutError as exc:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git discovery exceeded the configured timeout.",
                outcome="TIMEOUT",
            ) from exc
        self._assert_backend_current(session, workspace, execution_workspace)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise _GitError("GIT_OPERATION_FAILED", "Execution backend returned an invalid Git result.")
        if result.get("timed_out") is True:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git discovery exceeded the backend timeout.",
                outcome="TIMEOUT",
                exit_code=result.get("exit_code") if type(result.get("exit_code")) is int else None,
                output_truncated=result.get("truncated") is True,
            )
        if result.get("cancelled") is True:
            raise _GitError(
                "CANCELLED", "Git discovery was cancelled.", outcome="CANCELLED",
                exit_code=result.get("exit_code") if type(result.get("exit_code")) is int else None,
                output_truncated=result.get("truncated") is True,
            )
        exit_code = result.get("exit_code")
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if type(exit_code) is not int or not isinstance(stdout, str):
            raise _GitError("GIT_OPERATION_FAILED", "Git discovery returned an invalid result.")
        try:
            raw_stdout, lossy = _lossless_output(result, "stdout", stdout)
        except (ValueError, binascii.Error) as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git discovery returned invalid encoded output.") from exc
        if result.get("truncated") is True or result.get("terminated_by_output_limit") is True:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git discovery output was incomplete.",
                outcome="PARTIAL", exit_code=exit_code, output_truncated=True,
            )
        if lossy:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git discovery output was decoded lossily.",
                outcome="PARTIAL", exit_code=exit_code,
            )
        if exit_code == 0 and result.get("ok") is not True:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git discovery command failed without a nonzero exit status.",
                exit_code=exit_code,
            )
        if exit_code != 0 and not allow_failure:
            raise _GitError(
                "GIT_OPERATION_FAILED", "Git discovery command failed.",
                exit_code=exit_code,
            )
        return {
            "stdout": raw_stdout.decode("utf-8", errors="strict"),
            "stderr": stderr[:1024] if isinstance(stderr, str) else "",
            "exit_code": exit_code,
        }

    @staticmethod
    def _result(
        operation: str,
        capability: RepositoryCapability | None,
        workspace_identity: str | None,
        success: bool,
        records: list[dict[str, Any]],
        error_code: str | None,
        error: str | None,
        *,
        truncated: bool = False,
        limitations: list[str] | None = None,
        outcome: str | None = None,
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        if outcome is None:
            outcome = "COMPLETE" if success and not truncated else "PARTIAL" if success else "FAILED"
        complete = outcome == "COMPLETE"
        return {
            "operation": operation,
            "success": success,
            "ok": success,
            "repository_identity": capability.identity() if capability else None,
            "workspace_identity": workspace_identity,
            "records": records,
            "result_count": len(records),
            "truncated": truncated,
            "output_truncated": truncated,
            "complete": complete,
            "authoritative": complete,
            "outcome": outcome,
            "process_completed": exit_code is not None,
            "exit_code": exit_code,
            "timed_out": outcome == "TIMEOUT",
            "cancelled": outcome == "CANCELLED",
            "limitations": list(limitations or []),
            "error_code": error_code,
            "error": error,
        }
class _GitError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome: str = "FAILED",
        exit_code: int | None = None,
        output_truncated: bool = False,
    ) -> None:
        super().__init__(message[:512])
        self.code = code
        self.outcome = outcome
        self.exit_code = exit_code
        self.output_truncated = output_truncated


def validate_git_arguments(operation: str, arguments: object) -> bool:
    if not isinstance(arguments, dict) or operation not in GIT_ARGUMENTS:
        return False
    keys = set(arguments)
    if operation == "git_status":
        return not keys
    if operation == "git_diff":
        if keys != {"mode", "revision", "paths", "include_patch"}:
            return False
        mode, revision = arguments["mode"], arguments["revision"]
        return (
            mode in {"unstaged", "staged", "revision"}
            and isinstance(revision, str)
            and (revision == "" if mode != "revision" else _REVISION.fullmatch(revision) is not None)
            and isinstance(arguments["paths"], list)
            and len(arguments["paths"]) <= 64
            and all(_safe_git_path(path) for path in arguments["paths"])
            and len(set(arguments["paths"])) == len(arguments["paths"])
            and type(arguments["include_patch"]) is bool
        )
    if operation == "git_log":
        return keys == {"limit"} and type(arguments["limit"]) is int and 1 <= arguments["limit"] <= 100
    if operation == "git_show":
        if keys != {"revision", "path"}:
            return False
        return (
            isinstance(arguments["revision"], str)
            and _REVISION.fullmatch(arguments["revision"]) is not None
            and isinstance(arguments["path"], str)
            and (arguments["path"] == "" or _safe_git_path(arguments["path"]))
        )
    return False


def _safe_git_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if "\\" in value or "\x00" in value or value.startswith(":"):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _repo_paths(
    capability: RepositoryCapability,
    paths: list[str],
) -> list[str]:
    workspace = Path(capability.workspace_root)
    repository_relative = PurePosixPath(capability.repository_root or ".")
    repository_host_path = workspace.joinpath(*repository_relative.parts)
    result = []
    for value in paths:
        if not _safe_git_path(value):
            raise _GitError("UNSAFE_PATH", "Git paths must be safe workspace-relative paths.")
        full = (workspace / value).resolve(strict=False)
        if not full.is_relative_to(workspace) or not full.is_relative_to(repository_host_path):
            raise _GitError("UNSAFE_PATH", "Git path is outside the repository/workspace boundary.")
        repo_relative = full.relative_to(repository_host_path).as_posix()
        if not repo_relative or repo_relative == ".":
            raise _GitError("UNSAFE_PATH", "Git path must select a file or subdirectory.")
        result.append(repo_relative)
    return result


def _repo_path_from_workspace(
    capability: RepositoryCapability,
    workspace_path: str,
) -> str:
    workspace = Path(capability.workspace_root)
    repository_relative = PurePosixPath(capability.repository_root or ".")
    repository_host_path = workspace.joinpath(*repository_relative.parts)
    value = workspace / workspace_path
    resolved = value.resolve(strict=False)
    if not resolved.is_relative_to(workspace) or not resolved.is_relative_to(repository_host_path):
        raise _GitError("UNSAFE_PATH", "Git result path is outside the repository.")
    relative = resolved.relative_to(repository_host_path).as_posix()
    if not _safe_git_path(relative):
        raise _GitError("UNSAFE_PATH", "Git result path is unsafe.")
    return relative


def _workspace_path_from_repo(
    capability: RepositoryCapability,
    repo_path: str,
) -> str:
    workspace = Path(capability.workspace_root)
    root = Path(capability.repository_root or "")
    relative = PurePosixPath(repo_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in repo_path
        or "\x00" in repo_path
    ):
        raise _GitError("UNSAFE_PATH", "Git returned a path outside the repository.")
    workspace_relative = PurePosixPath(root.as_posix()) / relative
    if workspace_relative.is_absolute() or any(part == ".." for part in workspace_relative.parts):
        raise _GitError("UNSAFE_PATH", "Git returned a path outside the workspace.")
    candidate = workspace.joinpath(*workspace_relative.parts)
    if not candidate.resolve(strict=False).is_relative_to(workspace):
        raise _GitError("UNSAFE_PATH", "Git returned a path through a symlink outside the workspace.")
    return workspace_relative.as_posix()


def _lossless_output(result: dict[str, Any], name: str, text: str) -> tuple[bytes, bool]:
    encoded = result.get(f"{name}_base64")
    if encoded is not None:
        if not isinstance(encoded, str):
            raise ValueError("Encoded command output must be text")
        return base64.b64decode(encoded, validate=True), False
    return text.encode("utf-8", errors="surrogateescape"), "\ufffd" in text


def _parse_status(
    data: bytes,
    capability: RepositoryCapability,
) -> tuple[list[dict[str, Any]], bool]:
    fields = data.split(b"\0")
    terminated = not data or data.endswith(b"\0")
    if fields:
        fields.pop()
    records: list[dict[str, Any]] = []
    index = 0
    malformed = not terminated
    while index < len(fields) and fields[index]:
        field = fields[index]
        kind = field[:1]
        if kind == b"?":
            path = _decode_path(field[2:])
            records.append(_status_record(path, path, "untracked", "??", capability))
            index += 1
            continue
        if kind in {b"1", b"2"}:
            tokens = field.split(b" ", 8 if kind == b"1" else 9)
            required = 9 if kind == b"1" else 10
            if len(tokens) != required:
                malformed = True
                break
            xy = tokens[1].decode("ascii", errors="replace")
            original_or_path = _decode_path(tokens[-1])
            if kind == b"2":
                index += 1
                if index >= len(fields) or not fields[index]:
                    malformed = True
                    break
                source = _decode_path(fields[index])
                destination = original_or_path
                category = "renamed"
            else:
                source = destination = original_or_path
                category = _status_category(xy)
            records.append(_status_record(
                source, destination, category, xy, capability,
            ))
            index += 1
            continue
        if kind == b"u":
            tokens = field.split(b" ", 10)
            if len(tokens) != 11:
                malformed = True
                break
            xy = tokens[1].decode("ascii", errors="replace")
            path = _decode_path(tokens[-1])
            records.append(_status_record(path, path, "conflict", xy, capability))
            index += 1
            continue
        malformed = True
        break
    return records, malformed


def _status_category(xy: str) -> str:
    if "U" in xy or xy in {"AA", "DD"}:
        return "conflict"
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    if "R" in xy:
        return "renamed"
    if "C" in xy:
        return "copied"
    if xy == "??":
        return "untracked"
    return "modified"


def _status_record(
    source: str,
    destination: str,
    category: str,
    xy: str,
    capability: RepositoryCapability,
) -> dict[str, Any]:
    source_path = _workspace_path_from_repo(capability, source)
    destination_path = _workspace_path_from_repo(capability, destination)
    return {
        "source_path": source_path,
        "destination_path": destination_path if destination != source else None,
        "category": category,
        "index_status": xy[0] if xy else ".",
        "worktree_status": xy[1] if len(xy) > 1 else ".",
        "staged": bool(xy and xy[0] not in {".", "?"}),
        "unstaged": bool(len(xy) > 1 and xy[1] != "."),
    }


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _parse_name_status(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    fields = data.split(b"\0")
    terminated = not data or data.endswith(b"\0")
    if fields:
        fields.pop()
    records: list[dict[str, Any]] = []
    index = 0
    malformed = not terminated
    while index < len(fields) and fields[index]:
        status = fields[index].decode("ascii", errors="replace")
        index += 1
        if index >= len(fields) or not fields[index]:
            malformed = True
            break
        source = _decode_path(fields[index])
        index += 1
        destination = source
        if status.startswith(("R", "C")):
            if index >= len(fields) or not fields[index]:
                malformed = True
                break
            destination = _decode_path(fields[index])
            index += 1
        records.append({
            "source_path": source,
            "destination_path": destination if destination != source else None,
            "category": _diff_category(status),
        })
    return records, malformed


def _diff_category(status: str) -> str:
    return {
        "A": "added", "D": "deleted", "M": "modified", "T": "type_changed",
        "U": "conflict",
    }.get(status[:1], "renamed" if status.startswith("R") else "copied")


def _parse_numstat(data: bytes) -> tuple[dict[str, dict[str, Any]], bool]:
    fields = data.split(b"\0")
    terminated = not data or data.endswith(b"\0")
    if fields:
        fields.pop()
    result: dict[str, dict[str, Any]] = {}
    malformed = not terminated
    index = 0
    while index < len(fields) and fields[index]:
        columns = fields[index].split(b"\t", 2)
        if len(columns) != 3:
            malformed = True
            break
        index += 1
        path = columns[2]
        if not path:
            if index + 1 >= len(fields) or not fields[index] or not fields[index + 1]:
                malformed = True
                break
            old_path, new_path = _decode_path(fields[index]), _decode_path(fields[index + 1])
            index += 2
            path = new_path.encode("utf-8", errors="surrogateescape")
            del old_path
        added = columns[0].decode("ascii", errors="replace")
        deleted = columns[1].decode("ascii", errors="replace")
        try:
            result[_decode_path(path)] = {
                "additions": None if added == "-" else int(added),
                "deletions": None if deleted == "-" else int(deleted),
                "binary": added == "-" or deleted == "-",
            }
        except ValueError:
            malformed = True
            break
    return result, malformed


def _parse_log(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    malformed = False
    for entry in data.split(b"\x1e"):
        if not entry:
            continue
        fields = entry.lstrip(b"\n\0").split(b"\0")
        if len(fields) != 7 or fields[6].strip(b"\n"):
            malformed = True
            continue
        commit_bytes, parents, timestamp, author, email, subject = fields[:6]
        commit = commit_bytes.decode("ascii", errors="replace")
        if not _HEX.fullmatch(commit):
            malformed = True
            continue
        try:
            committed = int(timestamp)
        except ValueError:
            malformed = True
            continue
        author_text = author.decode("utf-8", errors="replace")
        email_text = email.decode("utf-8", errors="replace")
        subject_text = subject.decode("utf-8", errors="replace")
        records.append({
            "commit": commit,
            "parents": [item.decode("ascii", errors="replace") for item in parents.split()] if parents else [],
            "timestamp": committed,
            "author": author_text[:256],
            "email": email_text[:256],
            "subject": subject_text[:1024],
            "message": subject_text[:1024],
            "message_truncated": len(subject_text) > 1024,
        })
    return records[:_MAX_RECORDS], malformed or len(records) > _MAX_RECORDS


def encode_result(value: dict[str, Any]) -> str:
    """Stable bounded JSON rendering for tests and tool consumers."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
