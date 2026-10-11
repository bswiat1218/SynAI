from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from synai.broker.protocol import BrokerError, canonical_json
from synai.broker.filesystem import Directory, directory_access, read_file, walk_files, write_new


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


def stage_snapshot(task_root: Path | Directory, manifest: dict[str, Any], files: dict[str, bytes]) -> Path:
    with directory_access(task_root) as root:
        if os.listdir(root.fd):
            raise BrokerError("task_workspace_exists", "Assigned task directory is not empty.", 409)
        write_new(root, "manifest.json", canonical_json(manifest), 0o400)
        with root.mkdir("snapshot") as source, root.mkdir("workspace") as work:
            for entry in manifest["files"]:
                path = validate_relative_path(entry["path"])
                _stage_file(source, path, files[path], 0o400)
                _stage_file(work, path, files[path], 0o600)
            _make_immutable(source)
        return root.path / "workspace"


def _stage_file(root: Directory, path: str, data: bytes, mode: int) -> None:
    parts = PurePosixPath(path).parts
    if len(parts) == 1:
        write_new(root, parts[0], data, mode)
        return
    try:
        child = root.mkdir(parts[0])
    except FileExistsError:
        child = root.child_directory(parts[0])
    with child:
        _stage_file(child, "/".join(parts[1:]), data, mode)


def _make_immutable(directory: Directory) -> None:
    for name in os.listdir(directory.fd):
        info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with directory.child_directory(name) as child:
                _make_immutable(child)
        elif not stat.S_ISREG(info.st_mode):
            raise BrokerError("snapshot_file_invalid", "Snapshot entry changed during staging.", 409)
    directory.revalidate()
    os.fchmod(directory.fd, 0o500)
    os.fsync(directory.fd)


def load_staged_manifest(task_root: Path | Directory, expected_digest: str) -> dict[str, Any]:
    try:
        with directory_access(task_root) as root:
            data = read_file(root, "manifest.json", 2 * 1024 * 1024)
        manifest = json.loads(data)
        if hashlib.sha256(canonical_json(manifest)).hexdigest() != expected_digest:
            raise ValueError("Staged snapshot manifest digest changed")
        return manifest
    except (OSError, ValueError, TypeError) as exc:
        raise BrokerError("snapshot_integrity_error", "Staged immutable snapshot failed integrity validation.", 409) from exc


def changed_files(
    workspace: Path | Directory,
    manifest: dict[str, Any],
    maximum: int = MAX_SNAPSHOT_BYTES,
    output_limit: int = 1024 * 1024,
) -> list[dict[str, object]]:
    before = {entry["path"]: entry for entry in manifest["files"]}
    seen: set[str] = set()
    changes: list[dict[str, object]] = []
    total = 0
    encoded_output = 0
    with directory_access(workspace) as root:
        for relative, data in walk_files(root):
            relative = validate_relative_path(relative)
            if total + len(data) > maximum:
                raise BrokerError("artifact_limit", "Task artifacts exceed the configured bound.", 413)
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
