from __future__ import annotations

import re
import os
import secrets
import stat
import hashlib
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any

from synai.broker.protocol import BrokerError, OperationKind, canonical_json
from synai.broker.snapshots import MAX_FILE_BYTES, validate_relative_path
from synai.broker.filesystem import Directory, directory_access, open_beneath, mount_identity
from collections.abc import Callable


_COMMANDS = frozenset({"python3", "pytest", "git", "ruff", "npm"})
_ARG = re.compile(r"[A-Za-z0-9_./:=+@,-]{1,256}\Z")


def validate_operation(kind: str, arguments: object) -> dict[str, Any]:
    if kind not in {entry.value for entry in OperationKind} or not isinstance(arguments, dict):
        raise BrokerError("operation_invalid", "Operation kind or arguments are invalid.", 422)
    if kind == "read_file":
        if set(arguments) != {"path"}:
            raise BrokerError("operation_invalid", "Operation arguments do not match the file operation.", 422)
        return {"path": validate_relative_path(arguments["path"])}
    if kind == "list_files":
        if set(arguments) != {"path"}:
            raise BrokerError("operation_invalid", "Operation arguments do not match the list operation.", 422)
        value = arguments["path"]
        if value != ".":
            validate_relative_path(value)
        return {"path": value}
    if kind == "write_file":
        if set(arguments) != {"path", "content", "expected_sha256"}:
            raise BrokerError("operation_invalid", "Operation arguments do not match the write operation.", 422)
        path, content, digest = arguments["path"], arguments["content"], arguments["expected_sha256"]
        validate_relative_path(path)
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise BrokerError("operation_too_large", "File content exceeds its size limit.", 413)
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise BrokerError("operation_invalid", "File preimage digest is invalid.", 422)
        return dict(arguments)
    if kind == "patch_file":
        if set(arguments) != {"path", "old", "new", "expected_sha256"}:
            raise BrokerError("operation_invalid", "Operation arguments do not match the patch operation.", 422)
        validate_relative_path(arguments["path"])
        if (
            not isinstance(arguments["old"], str) or not arguments["old"]
            or not isinstance(arguments["new"], str)
            or len(arguments["old"].encode()) > MAX_FILE_BYTES
            or len(arguments["new"].encode()) > MAX_FILE_BYTES
        ):
            raise BrokerError("operation_too_large", "Patch arguments are invalid or exceed their bounds.", 413)
        if not isinstance(arguments["expected_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", arguments["expected_sha256"]):
            raise BrokerError("operation_invalid", "Patch requires an exact preimage digest.", 422)
        if arguments["new"] and len(arguments["new"].encode("utf-8")) > MAX_FILE_BYTES:
            raise BrokerError("operation_too_large", "Patch replacement exceeds its size limit.", 413)
        return dict(arguments)
    if kind == "delete_file":
        if set(arguments) != {"path", "expected_sha256"}:
            raise BrokerError("operation_invalid", "Delete requires a path and exact preimage digest.", 422)
        validate_relative_path(arguments["path"])
        if not isinstance(arguments["expected_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", arguments["expected_sha256"]):
            raise BrokerError("operation_invalid", "Delete requires an exact preimage digest.", 422)
        return dict(arguments)
    if kind == "command" or kind == "verify":
        if set(arguments) != {"argv", "cwd"}:
            raise BrokerError("operation_invalid", "Command arguments must contain argv and cwd only.", 422)
        argv, cwd = arguments["argv"], arguments["cwd"]
        if (
            not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or argv[0] not in _COMMANDS
            or any(not isinstance(arg, str) or not _ARG.fullmatch(arg) for arg in argv)
            or sum(len(arg.encode()) for arg in argv) > 4096
        ):
            raise BrokerError("command_invalid", "Command is outside the approved executable and argument grammar.", 422)
        validate_relative_path(cwd) if cwd != "." else None
        if any(token in {";", "&&", "||", "|", ">", "<", "$", "`"} for arg in argv for token in (arg,)):
            raise BrokerError("command_invalid", "Shell operators are not accepted in command arguments.", 422)
        return {"argv": argv, "cwd": cwd}
    if kind in {"prepare", "start", "status", "complete", "cancel", "terminate", "remove", "collect"}:
        if arguments:
            raise BrokerError("operation_invalid", "Lifecycle operation does not accept caller-selected options.", 422)
        return {}
    raise BrokerError("tool_not_allowed", "Operation kind is not supported.", 403)


def validate_claim_operation(claim_kind: str, request_kind: str, arguments: object) -> dict[str, Any]:
    if claim_kind != request_kind:
        raise BrokerError("operation_claim_mismatch", "Operation kind differs from the approved task claim.", 403)
    return validate_operation(request_kind, arguments)


def execute_file_operation(workspace: Path | Directory, kind: str, arguments: dict[str, Any], output_limit: int) -> dict[str, Any]:
    with directory_access(workspace) as authority:
        return _execute_file_operation(authority, kind, arguments, output_limit)


def _execute_file_operation(workspace: Directory, kind: str, arguments: dict[str, Any], output_limit: int) -> dict[str, Any]:
    """Perform bounded workspace-only file operations using no-follow dirfds."""
    arguments = validate_operation(kind, arguments)
    root_fd = os.dup(workspace.fd)
    parent_authority = workspace
    authorities: list[Directory] = []
    try:
        if arguments.get("path") != ".":
            for part in PurePosixPath(arguments["path"]).parts[:-1]:
                parent_authority = parent_authority.child_directory(part)
                authorities.append(parent_authority)
        parent_fd, name = _parent_fd(root_fd, arguments["path"]) if arguments.get("path") != "." else (os.dup(root_fd), ".")
        try:
            identity = mount_identity(parent_fd)

            def verify_parent() -> None:
                workspace.revalidate()
                fresh, _ = _parent_fd(root_fd, arguments["path"]) if arguments["path"] != "." else (os.dup(root_fd), ".")
                try:
                    if mount_identity(fresh) != identity:
                        raise BrokerError("filesystem_identity_changed", "Workspace parent was substituted.", 409)
                finally:
                    os.close(fresh)

            if kind == "list_files":
                target_fd = os.dup(root_fd) if name == "." else open_beneath(
                    parent_fd, name, os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    result = sorted(os.listdir(target_fd))
                    payload = canonical_json({"entries": result})
                    if len(payload) > output_limit:
                        raise BrokerError("operation_output_limit", "File listing exceeds its output limit.", 413)
                    return {"entries": result}
                finally:
                    os.close(target_fd)
            if kind == "read_file":
                data = _read_at(parent_fd, name, parent_authority)
                if len(data) > output_limit:
                    raise BrokerError("operation_output_limit", "File content exceeds its output limit.", 413)
                return {"content": data.decode("utf-8", errors="strict"), "sha256": hashlib.sha256(data).hexdigest()}
            if kind == "delete_file":
                data = _read_at(parent_fd, name, parent_authority)
                _check_preimage(data, arguments["expected_sha256"])
                verify_parent()
                os.unlink(name, dir_fd=parent_fd)
                parent_authority.forget_file(name)
                os.fsync(parent_fd)
                return {"deleted": True, "preimage_sha256": hashlib.sha256(data).hexdigest()}
            if kind == "write_file":
                data = arguments["content"].encode("utf-8")
                expected = arguments["expected_sha256"]
                existing = _read_optional_at(parent_fd, name, parent_authority)
                if (existing is None) != (expected is None):
                    raise BrokerError("preimage_conflict", "Workspace file existence changed before write.", 409)
                if existing is not None:
                    _check_preimage(existing, expected)
                _atomic_write(parent_fd, name, data, verify_parent, parent_authority)
                return {
                    "written": True,
                    "preimage_sha256": None if existing is None else hashlib.sha256(existing).hexdigest(),
                    "postimage_sha256": hashlib.sha256(data).hexdigest(),
                }
            if kind == "patch_file":
                existing = _read_at(parent_fd, name, parent_authority)
                _check_preimage(existing, arguments["expected_sha256"])
                text = existing.decode("utf-8", errors="strict")
                if text.count(arguments["old"]) != 1:
                    raise BrokerError("patch_conflict", "Patch text must match exactly once.", 409)
                updated = text.replace(arguments["old"], arguments["new"], 1).encode("utf-8")
                if len(updated) > MAX_FILE_BYTES:
                    raise BrokerError("operation_too_large", "Patched file exceeds its size limit.", 413)
                _atomic_write(parent_fd, name, updated, verify_parent, parent_authority)
                return {
                    "written": True,
                    "preimage_sha256": hashlib.sha256(existing).hexdigest(),
                    "postimage_sha256": hashlib.sha256(updated).hexdigest(),
                }
            raise BrokerError("tool_not_allowed", "Operation is not a scoped file operation.", 403)
        finally:
            os.close(parent_fd)
    except UnicodeDecodeError as exc:
        raise BrokerError("file_not_text", "Workspace file is not valid UTF-8 text.", 422) from exc
    except FileNotFoundError as exc:
        raise BrokerError("file_not_found", "Workspace file or directory was not found.", 404) from exc
    except OSError as exc:
        if exc.errno in {40, 20, 21}:
            raise BrokerError("unsafe_workspace_path", "Workspace path contains a symlink or wrong file type.", 422) from exc
        raise
    finally:
        for authority in reversed(authorities):
            authority.close()
        os.close(root_fd)


def _parent_fd(root_fd: int, path: str) -> tuple[int, str]:
    parts = PurePosixPath(validate_relative_path(path)).parts
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = open_beneath(
                current, component, os.O_RDONLY | os.O_DIRECTORY,
            )
            os.close(current)
            current = next_fd
            info = os.fstat(current)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise BrokerError("filesystem_ownership_invalid", "Workspace parent is untrusted.", 409)
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _read_optional_at(parent_fd: int, name: str, authority: Directory | None = None) -> bytes | None:
    try:
        descriptor = open_beneath(parent_fd, name, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if authority is not None:
            authority.verify_file(name, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES or info.st_uid != os.geteuid():
            raise BrokerError("file_type_unsupported", "Only bounded regular workspace files are supported.", 422)
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_FILE_BYTES:
            raise BrokerError("operation_too_large", "Workspace file exceeds its size limit.", 413)
        return bytes(data)
    finally:
        os.close(descriptor)


def _read_at(parent_fd: int, name: str, authority: Directory | None = None) -> bytes:
    data = _read_optional_at(parent_fd, name, authority)
    if data is None:
        raise FileNotFoundError(name)
    return data


def _check_preimage(data: bytes, expected: str) -> None:
    if not hmac_compare(hashlib.sha256(data).hexdigest(), expected):
        raise BrokerError("preimage_conflict", "Workspace file changed since its approved preimage.", 409)


def _atomic_write(parent_fd: int, name: str, data: bytes, verify_parent: Callable[[], None],
                  authority: Directory | None = None) -> None:
    verify_parent()
    temporary = f".synai-{secrets.token_hex(16)}.tmp"
    descriptor = open_beneath(
        parent_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
    )
    try:
        if authority is not None:
            authority.verify_file(temporary, descriptor)
        view = memoryview(data)
        while view:
            if authority is not None:
                authority.verify_file(temporary, descriptor)
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("Workspace write made no progress")
            view = view[count:]
        os.fsync(descriptor)
        if authority is not None:
            authority.verify_file(temporary, descriptor)
    except BaseException:
        if authority is None or authority.file_verifier is None:
            os.unlink(temporary, dir_fd=parent_fd)
        raise
    finally:
        os.close(descriptor)
    try:
        verify_parent()
        # Do not replace a symlink or directory even though the parent is held.
        existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode):
            raise BrokerError("file_type_unsupported", "Only regular files may be replaced.", 422)
    except FileNotFoundError:
        pass
    if authority is not None:
        authority.replace_file(temporary, name)
    else:
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.fsync(parent_fd)


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left, right)
