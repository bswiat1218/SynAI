from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from synai.broker.protocol import BrokerError, canonical_json


MAX_FILES = 500
MAX_FILE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_PATH_CHARS = 512
MAX_PATH_DEPTH = 16
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def validate_relative_path(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS
        or "\\" in value or "\x00" in value or value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise BrokerError("snapshot_path_invalid", "Snapshot path is not a safe relative path.", 422)
    path = PurePosixPath(value)
    if path.as_posix() != value or len(path.parts) > MAX_PATH_DEPTH or any(part in {"", ".", ".."} for part in path.parts):
        raise BrokerError("snapshot_path_invalid", "Snapshot path is not a safe relative path.", 422)
    return value


def validate_snapshot(manifest: object, expected_snapshot: str, expected_digest: str, files: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "project_id", "source_device_id", "workspace_binding_id",
        "snapshot_id", "created_at", "files", "total_bytes", "source",
    }:
        raise BrokerError("snapshot_manifest_invalid", "Snapshot manifest fields are invalid.", 422)
    if manifest["schema_version"] != 1 or manifest["snapshot_id"] != expected_snapshot:
        raise BrokerError("snapshot_identity_mismatch", "Snapshot identity does not match the authorized claim.", 403)
    body = canonical_json(manifest)
    if not _HASH.fullmatch(expected_digest) or hashlib.sha256(body).hexdigest() != expected_digest:
        raise BrokerError("snapshot_manifest_digest_invalid", "Snapshot manifest digest does not match.", 422)
    entries = manifest["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES or not isinstance(files, dict) or len(files) != len(entries):
        raise BrokerError("snapshot_manifest_invalid", "Snapshot file list is outside its bounds.", 422)
    normalized: set[str] = set()
    total = 0
    declared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "file_type", "size_bytes", "sha256"}:
            raise BrokerError("snapshot_manifest_invalid", "Snapshot file entry is invalid.", 422)
        path = validate_relative_path(entry["path"])
        normalized_path = unicodedata.normalize("NFC", path).casefold()
        if normalized_path in normalized or path in declared:
            raise BrokerError("snapshot_path_collision", "Snapshot paths collide.", 422)
        normalized.add(normalized_path)
        declared.add(path)
        size = entry["size_bytes"]
        digest = entry["sha256"]
        if (
            entry["file_type"] != "regular" or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)
            or path not in files or not isinstance(files[path], bytes)
        ):
            raise BrokerError("snapshot_file_invalid", "Snapshot contains an unsupported file.", 422)
        data = files[path]
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest or b"\0" in data:
            raise BrokerError("snapshot_file_digest_invalid", "Snapshot file failed integrity validation.", 422)
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BrokerError("snapshot_file_invalid", "Snapshot files must contain UTF-8 text.", 422) from exc
        total += size
    if total > MAX_SNAPSHOT_BYTES or manifest["total_bytes"] != total or set(files) != declared:
        raise BrokerError("snapshot_size_invalid", "Snapshot total size or file set is inconsistent.", 422)
    return manifest


def stage_snapshot(root: Path, task_id: str, manifest: dict[str, Any], files: dict[str, bytes]) -> Path:
    task_root = root / "tasks" / task_id
    if task_root.exists() or task_root.is_symlink():
        raise BrokerError("task_workspace_exists", "Task workspace already exists.", 409)
    task_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(task_root.parent, 0o700)
    task_root.mkdir(mode=0o700)
    source = task_root / "snapshot"
    work = task_root / "workspace"
    source.mkdir(mode=0o700)
    try:
        manifest_bytes = canonical_json(manifest)
        manifest_path = task_root / "manifest.json"
        manifest_descriptor = os.open(
            manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        try:
            view = memoryview(manifest_bytes)
            while view:
                written = os.write(manifest_descriptor, view)
                if written <= 0:
                    raise OSError("Snapshot manifest write made no progress")
                view = view[written:]
            os.fsync(manifest_descriptor)
        finally:
            os.close(manifest_descriptor)
        for entry in manifest["files"]:
            path = validate_relative_path(entry["path"])
            parent = source
            for component in PurePosixPath(path).parts[:-1]:
                parent = parent / component
                parent.mkdir(mode=0o700, exist_ok=True)
            target = parent / PurePosixPath(path).name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            try:
                view = memoryview(files[path])
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("Snapshot staging write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for current, dirs, _ in os.walk(source, topdown=False):
            for directory in dirs:
                os.chmod(Path(current) / directory, 0o500)
            os.chmod(current, 0o500)
        shutil.copytree(source, work, copy_function=shutil.copyfile, symlinks=False)
        for current, dirs, filenames in os.walk(work):
            os.chmod(current, 0o700)
            for name in dirs:
                os.chmod(Path(current) / name, 0o700)
            for name in filenames:
                os.chmod(Path(current) / name, 0o600)
        return work
    except BaseException:
        shutil.rmtree(task_root, ignore_errors=True)
        raise


def load_staged_manifest(root: Path, task_id: str, expected_digest: str) -> dict[str, Any]:
    path = root / "tasks" / task_id / "manifest.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                raise ValueError("Staged snapshot manifest is invalid")
            data = bytearray()
            while len(data) <= 2 * 1024 * 1024:
                piece = os.read(descriptor, min(65536, 2 * 1024 * 1024 + 1 - len(data)))
                if not piece:
                    break
                data.extend(piece)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Staged snapshot manifest exceeds its size limit")
        finally:
            os.close(descriptor)
        manifest = json.loads(data)
        if hashlib.sha256(canonical_json(manifest)).hexdigest() != expected_digest:
            raise ValueError("Staged snapshot manifest digest changed")
        return manifest
    except (OSError, ValueError, TypeError) as exc:
        raise BrokerError("snapshot_integrity_error", "Staged immutable snapshot failed integrity validation.", 409) from exc


def changed_files(
    workspace: Path,
    manifest: dict[str, Any],
    maximum: int = MAX_SNAPSHOT_BYTES,
    output_limit: int = 1024 * 1024,
) -> list[dict[str, object]]:
    before = {entry["path"]: entry for entry in manifest["files"]}
    seen: set[str] = set()
    changes: list[dict[str, object]] = []
    total = 0
    encoded_output = 0
    for current, dirs, files in os.walk(workspace, followlinks=False):
        for name in tuple(dirs):
            item = Path(current) / name
            if item.is_symlink():
                raise BrokerError("artifact_unsupported_file", "Workspace contains a symlink.", 422)
        for name in files:
            file_path = Path(current) / name
            relative = validate_relative_path(file_path.relative_to(workspace).as_posix())
            info = file_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise BrokerError("artifact_unsupported_file", "Workspace contains a special file.", 422)
            if info.st_size > MAX_FILE_BYTES or total + info.st_size > maximum:
                raise BrokerError("artifact_limit", "Task artifacts exceed the configured bound.", 413)
            descriptor = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                content = bytearray()
                while len(content) <= MAX_FILE_BYTES:
                    piece = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - len(content)))
                    if not piece:
                        break
                    content.extend(piece)
                data = bytes(content)
            finally:
                os.close(descriptor)
            if len(data) > MAX_FILE_BYTES:
                raise BrokerError("artifact_limit", "Task artifact file exceeds its configured bound.", 413)
            total += len(data)
            digest = hashlib.sha256(data).hexdigest()
            old = before.get(relative)
            seen.add(relative)
            if old is None or digest != old["sha256"]:
                encoded = base64.b64encode(data).decode("ascii")
                entry = {
                    "path": relative, "preimage_sha256": None if old is None else old["sha256"],
                    "postimage_sha256": digest, "size_bytes": len(data),
                    "artifact_base64": encoded,
                }
                encoded_output += len(canonical_json(entry))
                if encoded_output > output_limit:
                    raise BrokerError("artifact_output_limit", "Changed-file artifacts exceed the operation output limit.", 413)
                changes.append(entry)
    for path, entry in before.items():
        if path not in seen:
            changes.append({
                "path": path, "preimage_sha256": entry["sha256"],
                "postimage_sha256": None, "size_bytes": 0,
            })
            encoded_output += len(canonical_json(changes[-1]))
            if encoded_output > output_limit:
                raise BrokerError("artifact_output_limit", "Changed-file manifest exceeds the operation output limit.", 413)
    return sorted(changes, key=lambda item: str(item["path"]))
