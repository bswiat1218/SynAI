from __future__ import annotations

import asyncio
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
    error_code: str | None = None
    limitation: str | None = None

    def identity(self) -> dict[str, str | None]:
        return {
            "root": self.repository_root,
            "git_directory": self.git_directory,
        }


class GitInspector:
    """Bounded read-only Git inspection through the configured tool backend."""

    def __init__(self, backend: ExecutionBackend) -> None:
        self.backend = backend

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
                )
            handlers = {
                "git_status": self._status,
                "git_diff": self._diff,
                "git_log": self._log,
                "git_show": self._show,
            }
            records, truncated, limitations = await handlers[operation](
                capability, arguments,
            )
            return self._result(
                operation, capability, workspace_identity, True, records,
                None, None, truncated=truncated, limitations=limitations,
            )
        except _GitError as exc:
            return self._result(
                operation, capability, workspace_identity, False, [],
                exc.code, str(exc),
            )
        except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as exc:
            code = "GIT_UNAVAILABLE" if isinstance(exc, (OSError, TimeoutError)) else "GIT_OPERATION_FAILED"
            return self._result(
                operation, capability, workspace_identity, False, [],
                code, str(exc)[:512],
            )

    async def discover(self, session: Session | None) -> RepositoryCapability:
        backend = self.backend
        workspace = backend.workspace
        if session is None or workspace is None or not backend.matches(session):
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
        prefix = await self._run("rev-parse --show-toplevel", allow_failure=True)
        if prefix["exit_code"] != 0:
            if prefix["exit_code"] == 127:
                raise _GitError("GIT_UNAVAILABLE", "The Git executable is unavailable.")
            diagnostic = prefix["stderr"].lower()
            if "not a git repository" not in diagnostic and "cannot change to" not in diagnostic:
                raise _GitError(
                    "GIT_OPERATION_FAILED",
                    f"Git repository metadata is malformed or inaccessible: {prefix['stderr'][:384]}",
                )
            return RepositoryCapability(
                False, None, None, str(root), "NOT_GIT_REPOSITORY",
                "The selected workspace is not inside a discoverable Git repository.",
            )
        try:
            repo_root = Path(prefix["stdout"].strip()).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git returned an invalid repository root.") from exc
        if not repo_root.is_relative_to(root):
            return RepositoryCapability(
                False, str(repo_root), None, str(root), "UNSAFE_PATH",
                "The repository root is outside the selected workspace boundary.",
            )
        gitdir_result = await self._run("rev-parse --absolute-git-dir", allow_failure=True)
        if gitdir_result["exit_code"] != 0:
            return RepositoryCapability(
                False, str(repo_root), None, str(root), "GIT_OPERATION_FAILED",
                "Git metadata location could not be resolved.",
            )
        try:
            gitdir = Path(gitdir_result["stdout"].strip()).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git returned an invalid metadata path.") from exc
        if not gitdir.is_relative_to(root):
            return RepositoryCapability(
                False, str(repo_root), str(gitdir), str(root), "UNSAFE_PATH",
                "Git metadata is outside the selected workspace boundary.",
            )
        return RepositoryCapability(True, str(repo_root), str(gitdir), str(root))

    async def _status(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        del arguments
        output = await self._git(
            capability,
            "status --porcelain=v2 -z --untracked-files=all --renames",
        )
        raw = output["stdout"].encode("utf-8", errors="surrogateescape")
        records, malformed = _parse_status(
            raw,
            Path(capability.repository_root or ""),
            Path(capability.workspace_root),
        )
        truncated = output["truncated"] or len(records) > _MAX_RECORDS or malformed
        limitations = ["Status output was bounded or contained an incomplete record."] if truncated else []
        return records[:_MAX_RECORDS], truncated, limitations

    async def _diff(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
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
        changes, malformed_names = _parse_name_status(name_result["stdout"])
        counts, malformed_counts = _parse_numstat(numstat_result["stdout"])
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
        truncated = any(item["truncated"] for item in (name_result, numstat_result))
        if len(changes) > _MAX_RECORDS or malformed_names or malformed_counts:
            truncated = True
        limitations: list[str] = []
        if truncated:
            limitations.append("Change summary output was bounded or incomplete.")
        if arguments["include_patch"]:
            deadline = time.monotonic() + self.backend.settings.command_timeout
            for record in records[:16]:
                path = record["destination_path"] or record["source_path"]
                repo_path = _repo_path_from_workspace(
                    capability, path,
                )
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
                record["patch"] = (
                    None if record["binary"] else patch["stdout"][:_PATCH_LIMIT]
                )
                truncated = truncated or patch["truncated"]
                if patch["truncated"]:
                    record.setdefault("limitations", []).append(
                        "Patch content was truncated at the configured byte limit."
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
        return records, truncated, limitations

    async def _log(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        limit = arguments["limit"]
        pretty = "--format=%x1e%H%x00%P%x00%ct%x00%an%x00%ae%x00%s%x00"
        output = await self._git(
            capability, f"log -n {limit} {pretty}", output_limit=_OUTPUT_LIMIT,
        )
        records, malformed = _parse_log(output["stdout"])
        truncated = output["truncated"] or malformed
        return records, truncated, (
            ["Commit history output was bounded or incomplete."] if truncated else []
        )

    async def _show(
        self, capability: RepositoryCapability, arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
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
            "content_truncated": output["truncated"],
        }], output["truncated"], (
            ["Commit content was bounded and may be truncated."]
            if output["truncated"] else []
        )

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
        if result["exit_code"] != 0 or not _HEX.fullmatch(resolved):
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
        repo_root = Path(capability.repository_root or "")
        workspace = Path(capability.workspace_root)
        relative_cwd = repo_root.relative_to(workspace).as_posix()
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
        try:
            result = await asyncio.wait_for(
                self.backend.execute("terminal", {"command": command, "cwd": "."}),
                timeout=self.backend.settings.command_timeout,
            )
        except TimeoutError as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git operation exceeded the configured timeout.") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise _GitError("GIT_OPERATION_FAILED", "Execution backend returned an invalid Git result.")
        if result.get("timed_out") is True:
            raise _GitError("GIT_OPERATION_FAILED", "Git operation exceeded the backend timeout.")
        stdout = result.get("stdout")
        if not isinstance(stdout, str):
            raise _GitError("GIT_OPERATION_FAILED", "Git operation returned invalid output.")
        if result.get("ok") is not True and result.get("exit_code") == 0:
            raise _GitError("GIT_OPERATION_FAILED", "Git command failed or its output was truncated.")
        backend_truncated = result.get("truncated") is True
        if result.get("exit_code") != 0 and not allow_failure and not backend_truncated:
            message = result.get("stderr")
            detail = message[:384] if isinstance(message, str) else "Git command failed."
            raise _GitError("GIT_OPERATION_FAILED", detail or "Git command failed.")
        return {
            "stdout": stdout[:output_limit],
            "exit_code": result.get("exit_code"),
            "truncated": backend_truncated or len(stdout.encode(
                "utf-8", errors="replace",
            )) > output_limit,
        }

    async def _run(
        self,
        arguments: str,
        *,
        allow_failure: bool = False,
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
        try:
            result = await asyncio.wait_for(
                self.backend.execute("terminal", {"command": command, "cwd": "."}),
                timeout=self.backend.settings.command_timeout,
            )
        except TimeoutError as exc:
            raise _GitError("GIT_OPERATION_FAILED", "Git discovery exceeded the configured timeout.") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise _GitError("GIT_OPERATION_FAILED", "Execution backend returned an invalid Git result.")
        if result.get("timed_out") is True:
            raise _GitError("GIT_OPERATION_FAILED", "Git discovery exceeded the backend timeout.")
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if not isinstance(stdout, str):
            raise _GitError("GIT_OPERATION_FAILED", "Git discovery returned invalid output.")
        if result.get("exit_code") != 0 and not allow_failure:
            raise _GitError("GIT_OPERATION_FAILED", "Git discovery command failed.")
        return {
            "stdout": stdout[:_OUTPUT_LIMIT],
            "stderr": stderr[:1024] if isinstance(stderr, str) else "",
            "exit_code": result.get("exit_code"),
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
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "success": success,
            "ok": success,
            "repository_identity": capability.identity() if capability else None,
            "workspace_identity": workspace_identity,
            "records": records,
            "result_count": len(records),
            "truncated": truncated,
            "limitations": list(limitations or []),
            "error_code": error_code,
            "error": error,
        }


class _GitError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message[:512])
        self.code = code


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
    root = Path(capability.repository_root or "")
    result = []
    for value in paths:
        if not _safe_git_path(value):
            raise _GitError("UNSAFE_PATH", "Git paths must be safe workspace-relative paths.")
        full = (workspace / value).resolve(strict=False)
        if not full.is_relative_to(workspace) or not full.is_relative_to(root):
            raise _GitError("UNSAFE_PATH", "Git path is outside the repository/workspace boundary.")
        repo_relative = full.relative_to(root).as_posix()
        if not repo_relative or repo_relative == ".":
            raise _GitError("UNSAFE_PATH", "Git path must select a file or subdirectory.")
        result.append(repo_relative)
    return result


def _repo_path_from_workspace(
    capability: RepositoryCapability,
    workspace_path: str,
) -> str:
    workspace = Path(capability.workspace_root)
    root = Path(capability.repository_root or "")
    value = workspace / workspace_path
    if not value.is_relative_to(workspace):
        raise _GitError("UNSAFE_PATH", "Git result path is outside the workspace.")
    resolved = value.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise _GitError("UNSAFE_PATH", "Git result path is outside the repository.")
    relative = resolved.relative_to(root).as_posix()
    if not _safe_git_path(relative):
        raise _GitError("UNSAFE_PATH", "Git result path is unsafe.")
    return relative


def _workspace_path_from_repo(
    capability: RepositoryCapability,
    repo_path: str,
) -> str:
    workspace = Path(capability.workspace_root)
    root = Path(capability.repository_root or "")
    path = PurePosixPath(repo_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _GitError("UNSAFE_PATH", "Git returned a path outside the repository.")
    full = root / Path(*path.parts)
    if not full.is_relative_to(workspace):
        raise _GitError("UNSAFE_PATH", "Git returned a path outside the workspace.")
    return full.relative_to(workspace).as_posix()


def _parse_status(
    data: bytes,
    repo_root: Path,
    workspace_root: Path,
) -> tuple[list[dict[str, Any]], bool]:
    fields = data.split(b"\0")
    records: list[dict[str, Any]] = []
    index = 0
    malformed = bool(fields and fields[-1])
    while index < len(fields) and fields[index]:
        field = fields[index]
        kind = field[:1]
        if kind == b"?":
            path = _decode_path(field[2:])
            records.append(_status_record(path, path, "untracked", "??", repo_root, workspace_root))
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
                source, destination, category, xy, repo_root, workspace_root,
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
            records.append(_status_record(path, path, "conflict", xy, repo_root, workspace_root))
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
    repo_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    prefix = repo_root.relative_to(workspace_root)
    source_path = (prefix / PurePosixPath(source)).as_posix()
    destination_path = (prefix / PurePosixPath(destination)).as_posix()
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


def _parse_name_status(text: str) -> tuple[list[dict[str, Any]], bool]:
    fields = text.encode("utf-8", errors="surrogateescape").split(b"\0")
    records: list[dict[str, Any]] = []
    index = 0
    malformed = bool(fields and fields[-1])
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


def _parse_numstat(text: str) -> tuple[dict[str, dict[str, Any]], bool]:
    fields = text.encode("utf-8", errors="surrogateescape").split(b"\0")
    result: dict[str, dict[str, Any]] = {}
    malformed = bool(fields and fields[-1])
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
        result[_decode_path(path)] = {
            "additions": None if added == "-" else int(added),
            "deletions": None if deleted == "-" else int(deleted),
            "binary": added == "-" or deleted == "-",
        }
    return result, malformed


def _parse_log(text: str) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    malformed = False
    for entry in text.split("\x1e"):
        if not entry:
            continue
        fields = entry.lstrip("\n\0").split("\0")
        if len(fields) < 6:
            malformed = True
            continue
        commit, parents, timestamp, author, email, subject = fields[:6]
        if not _HEX.fullmatch(commit):
            malformed = True
            continue
        try:
            committed = int(timestamp)
        except ValueError:
            malformed = True
            continue
        records.append({
            "commit": commit,
            "parents": parents.split() if parents else [],
            "timestamp": committed,
            "author": author[:256],
            "email": email[:256],
            "subject": subject[:1024],
            "message": subject[:1024],
            "message_truncated": len(subject) > 1024,
        })
    return records[:_MAX_RECORDS], malformed or len(records) > _MAX_RECORDS


def encode_result(value: dict[str, Any]) -> str:
    """Stable bounded JSON rendering for tests and tool consumers."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
