from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from synai.storage import checked_path
from synai.web.database import MetadataDatabase
from synai.web.distributed import (
    DevicePrincipal,
    DistributedError,
    DistributedRegistry,
    canonical_json,
)


@dataclass(frozen=True)
class SnapshotLimits:
    max_files: int = 500
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_path_depth: int = 16
    max_path_length: int = 512
    max_chunk_bytes: int = 256 * 1024
    max_concurrent_uploads_per_device: int = 4
    max_concurrent_uploads_total: int = 64
    max_temporary_bytes: int = 256 * 1024 * 1024
    max_storage_bytes: int = 1024 * 1024 * 1024
    upload_deadline_seconds: int = 60 * 60
    retention_seconds: int = 30 * 24 * 60 * 60

    def validate(self) -> None:
        values = (
            self.max_files, self.max_file_bytes, self.max_total_bytes,
            self.max_path_depth, self.max_path_length, self.max_chunk_bytes,
            self.max_concurrent_uploads_per_device, self.max_concurrent_uploads_total,
            self.max_temporary_bytes,
            self.max_storage_bytes, self.upload_deadline_seconds, self.retention_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("Snapshot limits must be positive integers")
        if (
            self.max_files > 10_000 or self.max_file_bytes > 1024 * 1024
            or self.max_total_bytes > 512 * 1024 * 1024
            or self.max_path_depth > 32 or self.max_path_length > 4096
            or self.max_chunk_bytes > 1024 * 1024
            or self.max_temporary_bytes > 2 * 1024 * 1024 * 1024
            or self.max_storage_bytes > 16 * 1024 * 1024 * 1024
            or self.max_concurrent_uploads_per_device > 64
            or self.max_concurrent_uploads_total > 512
            or self.upload_deadline_seconds > 24 * 60 * 60
            or self.retention_seconds > 365 * 24 * 60 * 60
        ):
            raise ValueError("Snapshot limits exceed hard safety bounds")


class SnapshotStore:
    """Private bounded uploads and immutable content, separate from other Core data."""

    def __init__(
        self,
        database: MetadataDatabase,
        registry: DistributedRegistry,
        limits: SnapshotLimits | None = None,
    ) -> None:
        self.database = database
        self.registry = registry
        self.limits = limits or SnapshotLimits()
        self.limits.validate()
        self.root = checked_path(database.storage.root / "web-snapshots")
        self.uploads = checked_path(self.root / "uploads")
        self.snapshots = checked_path(self.root / "committed")
        self.views = checked_path(self.root / "source-views")
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self._private_directory(self.root)
        self._private_directory(self.uploads)
        self._private_directory(self.snapshots)
        self._private_directory(self.views)
        self.cleanup()

    def begin(
        self,
        principal: DevicePrincipal,
        project_id: str,
        binding_id: str,
        manifest: object,
        idempotency_key: str | None = None,
        now: int | None = None,
    ) -> dict[str, object]:
        timestamp = int(time.time()) if now is None else now
        self.cleanup(timestamp)
        self.registry.require_binding(project_id, str(principal.device_id), binding_id, timestamp)
        files, total = self._validate_client_manifest(manifest)
        idempotency_key = idempotency_key or os.urandom(24).hex()
        if (
            not isinstance(idempotency_key, str)
            or not 16 <= len(idempotency_key) <= 128
            or any(not (ch.isascii() and (ch.isalnum() or ch in "-_")) for ch in idempotency_key)
        ):
            raise DistributedError("idempotency_key_invalid", "Upload idempotency key is invalid.", 422)
        manifest_json = canonical_json({"files": files, "total_bytes": total}).decode("utf-8")
        idempotency_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM snapshot_uploads WHERE device_id = ? AND idempotency_key_hash = ?",
                (str(principal.device_id), idempotency_hash),
            ).fetchone()
        if existing is not None:
            if (
                existing["project_id"] != project_id
                or existing["binding_id"] != binding_id
                or not hmac.compare_digest(existing["manifest_json"], manifest_json)
            ):
                raise DistributedError("idempotency_conflict", "Upload idempotency key was already used for different content.", 409)
            if existing["state"] == "expired":
                raise DistributedError("upload_expired", "Snapshot upload deadline has passed.", 410)
            return self._upload_summary(existing, reused=True)
        upload_id = os.urandom(16).hex()
        directory = self.uploads / upload_id
        with self._lock:
            try:
                self._private_directory(directory)
                for index, _ in enumerate(files):
                    path = directory / f"file-{index:04d}.part"
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600,
                    )
                    os.close(descriptor)
            except BaseException:
                self._remove_tree(directory)
                raise
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = connection.execute(
                        "SELECT * FROM snapshot_uploads WHERE device_id = ? AND idempotency_key_hash = ?",
                        (str(principal.device_id), idempotency_hash),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["project_id"] != project_id
                            or existing["binding_id"] != binding_id
                            or not hmac.compare_digest(existing["manifest_json"], manifest_json)
                        ):
                            raise DistributedError("idempotency_conflict", "Upload idempotency key was already used for different content.", 409)
                        connection.commit()
                        self._remove_tree(directory)
                        return self._upload_summary(existing, reused=True)
                    active = connection.execute(
                        "SELECT count(*) AS count, coalesce(sum(reserved_bytes), 0) AS bytes "
                        "FROM snapshot_uploads WHERE state IN ('receiving', 'committing') "
                        "AND deadline > ?",
                        (timestamp,),
                    ).fetchone()
                    retained = connection.execute(
                        "SELECT coalesce(sum(total_bytes), 0) AS bytes FROM immutable_snapshots "
                        "WHERE state = 'available' AND expires_at > ?",
                        (timestamp,),
                    ).fetchone()["bytes"]
                    own_active = connection.execute(
                        "SELECT count(*) AS count FROM snapshot_uploads "
                        "WHERE device_id = ? AND state IN ('receiving', 'committing') AND deadline > ?",
                        (str(principal.device_id), timestamp),
                    ).fetchone()["count"]
                    if active["count"] >= self.limits.max_concurrent_uploads_total:
                        raise DistributedError("upload_capacity", "Snapshot upload capacity is full.", 429)
                    if own_active >= self.limits.max_concurrent_uploads_per_device:
                        raise DistributedError("upload_capacity", "Device upload capacity is full.", 429)
                    if active["bytes"] + total > self.limits.max_temporary_bytes:
                        raise DistributedError("temporary_storage_full", "Temporary snapshot storage quota is full.", 413)
                    if retained + active["bytes"] + total > self.limits.max_storage_bytes:
                        raise DistributedError("snapshot_storage_full", "Snapshot retention storage quota is full.", 413)
                    connection.execute(
                        "INSERT INTO snapshot_uploads(upload_id, project_id, device_id, binding_id, "
                        "idempotency_key_hash, state, manifest_json, reserved_bytes, created_at, deadline) "
                        "VALUES (?, ?, ?, ?, ?, 'receiving', ?, ?, ?, ?)",
                        (
                            upload_id, project_id, str(principal.device_id), binding_id,
                            idempotency_hash, manifest_json,
                            total, timestamp, timestamp + self.limits.upload_deadline_seconds,
                        ),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    self._remove_tree(directory)
                    raise
        return {
            "upload_id": upload_id,
            "state": "receiving",
            "chunk_bytes": self.limits.max_chunk_bytes,
            "deadline": timestamp + self.limits.upload_deadline_seconds,
            "files": len(files),
            "total_bytes": total,
            "reused": False,
        }

    def accept_chunk(
        self,
        principal: DevicePrincipal,
        upload_id: str,
        file_index: int,
        chunk_index: int,
        body: bytes,
        now: int | None = None,
    ) -> dict[str, object]:
        timestamp = int(time.time()) if now is None else now
        _require_id(upload_id)
        if (
            type(file_index) is not int or file_index < 0
            or type(chunk_index) is not int or chunk_index < 0
            or not isinstance(body, bytes) or not 1 <= len(body) <= self.limits.max_chunk_bytes
        ):
            raise DistributedError("chunk_invalid", "Snapshot chunk is outside supported bounds.", 422)
        with self._lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload = self._authorized_upload(connection, principal, upload_id, timestamp)
                files = json.loads(upload["manifest_json"])["files"]
                if file_index >= len(files):
                    raise DistributedError("chunk_invalid", "Snapshot file index is invalid.", 422)
                entry = files[file_index]
                chunk_count = (entry["size_bytes"] + self.limits.max_chunk_bytes - 1) // self.limits.max_chunk_bytes
                expected_size = min(
                    self.limits.max_chunk_bytes,
                    entry["size_bytes"] - chunk_index * self.limits.max_chunk_bytes,
                )
                if chunk_index >= chunk_count or len(body) != expected_size:
                    raise DistributedError("chunk_invalid", "Snapshot chunk length or index is invalid.", 422)
                digest = hashlib.sha256(body).hexdigest()
                existing = connection.execute(
                    "SELECT byte_length, sha256 FROM snapshot_upload_chunks "
                    "WHERE upload_id = ? AND file_index = ? AND chunk_index = ?",
                    (upload_id, file_index, chunk_index),
                ).fetchone()
                if existing is not None:
                    if existing["byte_length"] != len(body) or existing["sha256"] != digest:
                        raise DistributedError("chunk_conflict", "Chunk index was already submitted with different bytes.", 409)
                    connection.commit()
                    return {"accepted": True, "duplicate": True}
                previous = connection.execute(
                    "SELECT max(chunk_index) AS last FROM snapshot_upload_chunks "
                    "WHERE upload_id = ? AND file_index = ?",
                    (upload_id, file_index),
                ).fetchone()["last"]
                if chunk_index != (0 if previous is None else previous + 1):
                    raise DistributedError("chunk_out_of_order", "Snapshot chunks must be submitted in order.", 409)
                path = self.uploads / upload_id / f"file-{file_index:04d}.part"
                self._write_chunk(path, body, chunk_index * self.limits.max_chunk_bytes)
                connection.execute(
                    "INSERT INTO snapshot_upload_chunks"
                    "(upload_id, file_index, chunk_index, byte_length, sha256) VALUES (?, ?, ?, ?, ?)",
                    (upload_id, file_index, chunk_index, len(body), digest),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"accepted": True, "duplicate": False}

    def commit(
        self,
        principal: DevicePrincipal,
        upload_id: str,
        now: int | None = None,
    ) -> dict[str, object]:
        timestamp = int(time.time()) if now is None else now
        _require_id(upload_id)
        with self._lock:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = connection.execute(
                        "SELECT * FROM snapshot_uploads WHERE upload_id = ? AND device_id = ?",
                        (upload_id, str(principal.device_id)),
                    ).fetchone()
                    if existing is not None and existing["state"] == "committed":
                        connection.commit()
                        return self.snapshot_status(
                            existing["project_id"], existing["snapshot_id"], now=timestamp,
                        )
                    upload = self._authorized_upload(connection, principal, upload_id, timestamp)
                    files = json.loads(upload["manifest_json"])["files"]
                    directory = self.uploads / upload_id
                    source_files: list[tuple[dict[str, object], bytes]] = []
                    for index, entry in enumerate(files):
                        path = directory / f"file-{index:04d}.part"
                        data = self._read_private_file(path, self.limits.max_file_bytes)
                        if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                            raise DistributedError("snapshot_integrity_error", "Uploaded source does not match its declared content digest.", 422)
                        if b"\x00" in data:
                            raise DistributedError("snapshot_file_type_unsupported", "Only UTF-8 text source files are accepted.", 422)
                        try:
                            data.decode("utf-8", errors="strict")
                        except UnicodeDecodeError as exc:
                            raise DistributedError("snapshot_file_type_unsupported", "Only UTF-8 text source files are accepted.", 422) from exc
                        chunk_count = (len(data) + self.limits.max_chunk_bytes - 1) // self.limits.max_chunk_bytes
                        count = connection.execute(
                            "SELECT count(*) AS count FROM snapshot_upload_chunks "
                            "WHERE upload_id = ? AND file_index = ?",
                            (upload_id, index),
                        ).fetchone()["count"]
                        if count != chunk_count:
                            raise DistributedError("snapshot_incomplete", "Snapshot upload is incomplete.", 409)
                        source_files.append((entry, data))
                    temporary = connection.execute(
                        "SELECT coalesce(sum(reserved_bytes), 0) AS bytes FROM snapshot_uploads "
                        "WHERE state IN ('receiving', 'committing') AND deadline > ?",
                        (timestamp,),
                    ).fetchone()["bytes"]
                    if temporary + upload["reserved_bytes"] > self.limits.max_temporary_bytes:
                        raise DistributedError("temporary_storage_full", "Snapshot commit would exceed temporary storage quota.", 413)
                    retained = connection.execute(
                        "SELECT coalesce(sum(total_bytes), 0) AS bytes FROM immutable_snapshots "
                        "WHERE state = 'available' AND expires_at > ?",
                        (timestamp,),
                    ).fetchone()["bytes"]
                    if retained + temporary + upload["reserved_bytes"] > self.limits.max_storage_bytes:
                        raise DistributedError("snapshot_storage_full", "Snapshot commit would exceed storage quota.", 413)
                    snapshot_id = os.urandom(16).hex()
                    key_fingerprint = connection.execute(
                        "SELECT key_fingerprint FROM paired_devices WHERE device_id = ?",
                        (str(principal.device_id),),
                    ).fetchone()["key_fingerprint"]
                    manifest = {
                        "schema_version": 1,
                        "project_id": upload["project_id"],
                        "source_device_id": upload["device_id"],
                        "workspace_binding_id": upload["binding_id"],
                        "snapshot_id": snapshot_id,
                        "created_at": timestamp,
                        "files": files,
                        "total_bytes": upload["reserved_bytes"],
                        "source": {
                            "kind": "paired_device",
                            "key_fingerprint": key_fingerprint,
                            "protocol_version": principal.protocol_version,
                        },
                    }
                    manifest_bytes = canonical_json(manifest)
                    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
                    staging = self.snapshots / f".{snapshot_id}.tmp"
                    final_dir = self.snapshots / snapshot_id
                    self._private_directory(staging)
                    try:
                        self._write_snapshot_tree(staging, source_files, manifest_bytes)
                        os.replace(staging, final_dir)
                        self._fsync_directory(self.snapshots)
                        connection.execute(
                            "INSERT INTO immutable_snapshots(snapshot_id, schema_version, project_id, "
                            "device_id, binding_id, manifest_json, manifest_digest, total_bytes, "
                            "state, created_at, expires_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?, "
                            "'available', ?, ?)",
                            (
                                snapshot_id, upload["project_id"], upload["device_id"],
                                upload["binding_id"], manifest_bytes.decode("utf-8"), manifest_digest,
                                upload["reserved_bytes"], timestamp,
                                timestamp + self.limits.retention_seconds,
                            ),
                        )
                        connection.execute(
                            "UPDATE snapshot_uploads SET state = 'committed', snapshot_id = ? "
                            "WHERE upload_id = ? AND state = 'receiving'",
                            (snapshot_id, upload_id),
                        )
                        connection.execute(
                            "DELETE FROM snapshot_upload_chunks WHERE upload_id = ?", (upload_id,),
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        self._remove_tree(staging)
                        self._remove_tree(final_dir)
                        raise
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
            self._remove_tree(self.uploads / upload_id)
        return self.snapshot_status(upload["project_id"], snapshot_id, now=timestamp)

    def snapshot_status(
        self,
        project_id: str,
        snapshot_id: str,
        now: int | None = None,
    ) -> dict[str, object]:
        _require_id(snapshot_id)
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id, project_id, device_id, binding_id, manifest_json, "
                "manifest_digest, total_bytes, state, created_at, expires_at FROM immutable_snapshots "
                "WHERE snapshot_id = ? AND project_id = ?",
                (snapshot_id, project_id),
            ).fetchone()
        if row is None:
            raise DistributedError("snapshot_not_found", "Snapshot was not found for this project.", 404)
        with self.database.connect() as connection:
            active_reference = connection.execute(
                "SELECT 1 FROM distributed_tasks WHERE snapshot_id = ? "
                "AND state IN ('queued', 'claimed') LIMIT 1",
                (snapshot_id,),
            ).fetchone()
        if row["state"] != "available" or (
            row["expires_at"] <= timestamp and active_reference is None
        ):
            raise DistributedError("snapshot_expired", "Snapshot is expired or unavailable.", 410)
        return {
            "snapshot_id": row["snapshot_id"],
            "project_id": row["project_id"],
            "source_device_id": row["device_id"],
            "workspace_binding_id": row["binding_id"],
            "state": "available",
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "total_bytes": row["total_bytes"],
            "manifest_digest": row["manifest_digest"],
            "manifest": json.loads(row["manifest_json"]),
        }

    def upload_status(
        self,
        principal: DevicePrincipal,
        upload_id: str,
        now: int | None = None,
    ) -> dict[str, object]:
        timestamp = int(time.time()) if now is None else now
        _require_id(upload_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshot_uploads WHERE upload_id = ? AND device_id = ?",
                (upload_id, str(principal.device_id)),
            ).fetchone()
        if row is None:
            raise DistributedError("upload_not_found", "Snapshot upload was not found.", 404)
        if row["state"] == "expired" or row["deadline"] <= timestamp and row["state"] != "committed":
            raise DistributedError("upload_expired", "Snapshot upload deadline has passed.", 410)
        data = json.loads(row["manifest_json"])
        return {
            "upload_id": row["upload_id"],
            "state": row["state"],
            "deadline": row["deadline"],
            "files": len(data["files"]),
            "total_bytes": row["reserved_bytes"],
            "snapshot_id": row["snapshot_id"],
        }

    def list_snapshots(self, project_id: str, now: int | None = None) -> tuple[dict[str, object], ...]:
        with self.database.connect() as connection:
            ids = connection.execute(
                "SELECT snapshot_id FROM immutable_snapshots WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT 256",
                (project_id,),
            ).fetchall()
        return tuple(self.snapshot_status(project_id, row["snapshot_id"], now) for row in ids)

    def expire(self, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else now
        with self._lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE snapshot_uploads SET state = 'expired' "
                    "WHERE state IN ('receiving', 'committing') AND deadline <= ?",
                    (timestamp,),
                )
                connection.execute(
                    "UPDATE immutable_snapshots SET state = 'expired' WHERE state = 'available' "
                    "AND expires_at <= ? AND NOT EXISTS (SELECT 1 FROM distributed_tasks t "
                    "WHERE t.snapshot_id = immutable_snapshots.snapshot_id "
                    "AND t.state IN ('queued', 'claimed'))",
                    (timestamp,),
                )
                connection.commit()
                expired = connection.execute(
                    "SELECT upload_id FROM snapshot_uploads WHERE state = 'expired' AND deadline <= ?",
                    (timestamp,),
                ).fetchall()
                snapshots = connection.execute(
                    "SELECT snapshot_id FROM immutable_snapshots WHERE state = 'expired'",
                ).fetchall()
            except BaseException:
                connection.rollback()
                raise
        for row in expired:
            self._remove_tree(self.uploads / row["upload_id"])
        for row in snapshots:
            self._remove_tree(self.snapshots / row["snapshot_id"])
            self._remove_tree(self.views / row["snapshot_id"])
        return len(expired) + len(snapshots)

    def cleanup(self, now: int | None = None) -> int:
        removed = self.expire(now)
        self._remove_orphan_upload_dirs()
        self._remove_orphaned_snapshot_dirs()
        return removed

    def _upload_summary(self, row: sqlite3.Row, *, reused: bool) -> dict[str, object]:
        return {
            "upload_id": row["upload_id"],
            "state": row["state"],
            "chunk_bytes": self.limits.max_chunk_bytes,
            "deadline": row["deadline"],
            "files": len(json.loads(row["manifest_json"])["files"]),
            "total_bytes": row["reserved_bytes"],
            "reused": reused,
        }

    def open_source(self, project_id: str, snapshot_id: str, now: int | None = None) -> SnapshotSource:
        return SnapshotSource(self, self.snapshot_status(project_id, snapshot_id, now))

    def _validate_client_manifest(self, value: object) -> tuple[list[dict[str, object]], int]:
        if not isinstance(value, list) or not 1 <= len(value) <= self.limits.max_files:
            raise DistributedError("snapshot_manifest_invalid", "Snapshot file list is outside supported bounds.", 422)
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        total = 0
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != {"path", "file_type", "size_bytes", "sha256"}:
                raise DistributedError("snapshot_manifest_invalid", "Snapshot file metadata is invalid.", 422)
            path = entry["path"]
            self._validate_path(path)
            canonical = unicodedata.normalize("NFC", path).casefold()
            if canonical in seen:
                raise DistributedError("snapshot_path_collision", "Snapshot contains duplicate or platform-colliding paths.", 422)
            seen.add(canonical)
            components = PurePosixPath(path).parts
            excluded_directories = {
                ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
                "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
                ".nox", ".cache", "dist", "build", "htmlcov", "coverage",
                "site-packages", ".ssh", ".aws",
            }
            filename = PurePosixPath(path).name.lower()
            excluded_names = {
                ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
                "id_rsa", "id_ed25519", "credentials", "secrets",
            }
            suffix_lower = PurePosixPath(path).suffix.lower()
            if (
                any(part in excluded_directories for part in components[:-1])
                or filename in excluded_names
                or filename.startswith(".env.") and filename not in {
                    ".env.example", ".env.sample", ".env.template",
                }
                or suffix_lower in {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der"}
            ):
                raise DistributedError("snapshot_path_excluded", "Snapshot path matches a protected exclusion rule.", 422)
            if entry["file_type"] != "regular":
                raise DistributedError("snapshot_file_type_unsupported", "Only regular files are accepted.", 422)
            suffix = PurePosixPath(path).suffix.lower()
            supported = {
                ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".ini", ".cfg",
                ".yaml", ".yml", ".json", ".xml", ".html", ".css", ".js", ".jsx",
                ".ts", ".tsx", ".sh", ".sql", ".go", ".rs", ".java", ".c", ".h",
                ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua",
            }
            if suffix not in supported and PurePosixPath(path).name not in {
                "Dockerfile", "Makefile", "GNUmakefile", "Justfile", "Procfile",
                ".gitignore", ".dockerignore", ".editorconfig", ".coveragerc",
                ".env.example", ".env.sample", ".env.template",
            }:
                raise DistributedError("snapshot_file_type_unsupported", "Snapshot file extension is unsupported.", 422)
            size = entry["size_bytes"]
            digest = entry["sha256"]
            if type(size) is not int or not 0 <= size <= self.limits.max_file_bytes:
                raise DistributedError("snapshot_file_too_large", "Snapshot file size is outside supported bounds.", 413)
            if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise DistributedError("snapshot_manifest_invalid", "Snapshot file digest is invalid.", 422)
            total += size
            if total > self.limits.max_total_bytes:
                raise DistributedError("snapshot_too_large", "Total snapshot size exceeds the configured limit.", 413)
            result.append({"path": path, "file_type": "regular", "size_bytes": size, "sha256": digest})
        return result, total

    def _validate_path(self, path: object) -> None:
        if not isinstance(path, str):
            raise DistributedError("snapshot_path_invalid", "Snapshot path is invalid.", 422)
        try:
            byte_length = len(path.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise DistributedError("snapshot_path_invalid", "Snapshot path is invalid.", 422) from exc
        if (
            not path or len(path) > self.limits.max_path_length
            or "\\" in path or "\x00" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or byte_length > self.limits.max_path_length
        ):
            raise DistributedError("snapshot_path_invalid", "Snapshot path is invalid.", 422)
        parsed = PurePosixPath(path)
        windows_reserved = {
            "CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
        if (
            parsed.is_absolute() or parsed.as_posix() != path
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or len(parsed.parts) > self.limits.max_path_depth
            or path.startswith("//")
            or re.match(r"^[A-Za-z]:", path) is not None
            or any(
                ":" in part or part.endswith((".", " "))
                or part.split(".", 1)[0].upper() in windows_reserved
                for part in parsed.parts
            )
        ):
            raise DistributedError("snapshot_path_invalid", "Snapshot path must be safe and workspace-relative.", 422)

    def _authorized_upload(
        self,
        connection: sqlite3.Connection,
        principal: DevicePrincipal,
        upload_id: str,
        timestamp: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM snapshot_uploads WHERE upload_id = ? AND device_id = ?",
            (upload_id, str(principal.device_id)),
        ).fetchone()
        if row is None:
            raise DistributedError("upload_not_found", "Snapshot upload was not found.", 404)
        if row["state"] != "receiving":
            raise DistributedError("upload_state_invalid", "Snapshot upload is not accepting data.", 409)
        if row["deadline"] <= timestamp:
            raise DistributedError("upload_expired", "Snapshot upload deadline has passed.", 410)
        self.registry.require_binding(row["project_id"], row["device_id"], row["binding_id"], timestamp)
        return row

    @staticmethod
    def _write_chunk(path: Path, data: bytes, offset: int) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            position = offset
            view = memoryview(data)
            while view:
                written = os.pwrite(descriptor, view, position)
                if written <= 0:
                    raise OSError("Snapshot chunk write made no progress")
                position += written
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_private_file(path: Path, maximum: int) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise DistributedError("snapshot_integrity_error", "Temporary snapshot object is invalid.", 422)
            pieces: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                pieces.append(chunk)
                remaining -= len(chunk)
            data = b"".join(pieces)
            if len(data) > maximum:
                raise DistributedError("snapshot_integrity_error", "Temporary snapshot object exceeds its bound.", 422)
            return data
        finally:
            os.close(descriptor)

    def _write_snapshot_tree(
        self,
        staging: Path,
        files: list[tuple[dict[str, object], bytes]],
        manifest: bytes,
    ) -> None:
        objects = staging / "objects"
        objects.mkdir(mode=0o700)
        for index, (_, data) in enumerate(files):
            path = objects / f"{index:04d}"
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o400,
            )
            try:
                self._write_all(descriptor, data)
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
            finally:
                os.close(descriptor)
        manifest_path = staging / "manifest.json"
        descriptor = os.open(
            manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        try:
            self._write_all(descriptor, manifest)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
        finally:
            os.close(descriptor)
        os.chmod(objects, 0o500)
        os.chmod(staging, 0o500)
        self._fsync_directory(staging)

    def _materialize_view(
        self,
        snapshot_id: str,
        manifest: dict[str, Any],
        files: list[tuple[dict[str, object], bytes]],
    ) -> None:
        view = self.views / snapshot_id
        with self._lock:
            if view.exists():
                self._verify_source_view(view, manifest)
                return
            staging = self.views / f".{snapshot_id}.tmp"
            self._private_directory(staging)
            try:
                for entry, data in files:
                    components = PurePosixPath(str(entry["path"])).parts
                    parent = staging
                    for component in components[:-1]:
                        parent = parent / component
                        if not parent.exists():
                            parent.mkdir(mode=0o700)
                        info = parent.lstat()
                        if not stat.S_ISDIR(info.st_mode):
                            raise DistributedError("snapshot_integrity_error", "Source view contains an unsafe directory.", 500)
                    target = parent / components[-1]
                    descriptor = os.open(
                        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o400,
                    )
                    try:
                        self._write_all(descriptor, data)
                        os.fsync(descriptor)
                        os.fchmod(descriptor, 0o400)
                    finally:
                        os.close(descriptor)
                for current, dirs, _ in os.walk(staging, topdown=False):
                    for directory in dirs:
                        os.chmod(Path(current) / directory, 0o500)
                    os.chmod(current, 0o500)
                os.replace(staging, view)
                self._fsync_directory(self.views)
            except BaseException:
                self._remove_tree(staging)
                raise

    def _verify_source_view(self, view: Path, manifest: dict[str, Any]) -> None:
        from synai.intelligence import RepositoryIndex

        try:
            info = view.lstat()
        except OSError as exc:
            raise DistributedError("snapshot_integrity_error", "Committed source view is unavailable.", 410) from exc
        if (
            not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077
        ):
            raise DistributedError("snapshot_integrity_error", "Committed source view is unsafe.", 410)
        index = RepositoryIndex(view)
        for entry in manifest["files"]:
            try:
                data = index.read_file_bytes(
                    entry["path"], max_bytes=self.limits.max_file_bytes,
                )
            except OSError as exc:
                raise DistributedError("snapshot_integrity_error", "Committed source view is unsafe or unavailable.", 410) from exc
            if (
                data is None or len(data) != entry["size_bytes"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise DistributedError("snapshot_integrity_error", "Committed source view changed.", 410)

    def _remove_orphaned_snapshot_dirs(self) -> None:
        with self.database.connect() as connection:
            known = {
                row["snapshot_id"]
                for row in connection.execute("SELECT snapshot_id FROM immutable_snapshots")
            }
        for path in self.snapshots.iterdir():
            if path.name not in known:
                self._remove_tree(path)

    def _remove_orphan_upload_dirs(self) -> None:
        with self.database.connect() as connection:
            known = {
                row["upload_id"]
                for row in connection.execute(
                    "SELECT upload_id FROM snapshot_uploads WHERE state = 'receiving' "
                    "AND deadline > ?",
                    (int(time.time()),),
                )
            }
        for path in self.uploads.iterdir():
            if path.name not in known:
                self._remove_tree(path)

    @classmethod
    def _private_directory(cls, path: Path) -> None:
        checked_path(path)
        if path.exists():
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Snapshot storage directory must be private and owned by this user")
            return
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            cls._private_directory(path)

    @classmethod
    def _remove_tree(cls, path: Path) -> None:
        if path.is_symlink():
            raise ValueError("Snapshot cleanup refuses symlink paths")
        if path.exists():
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                for current, directories, _ in os.walk(path, topdown=False, followlinks=False):
                    for directory in directories:
                        child = Path(current) / directory
                        child_info = child.lstat()
                        if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
                            os.chmod(child, 0o700)
                    os.chmod(current, 0o700)
                shutil.rmtree(path)
            else:
                path.unlink()

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Snapshot write made no progress")
            view = view[written:]

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SnapshotSource:
    """Read-only adapter from a committed snapshot into Phase 2/3 repository intelligence."""

    def __init__(self, store: SnapshotStore, status: dict[str, object]) -> None:
        from synai.intelligence import RepositoryIndex

        self.store = store
        self.status = status
        self.manifest = status["manifest"]
        self.root = store.views / str(status["snapshot_id"])
        expected = hashlib.sha256(canonical_json(self.manifest)).hexdigest()
        if expected != status["manifest_digest"]:
            raise DistributedError("snapshot_integrity_error", "Committed snapshot manifest failed integrity validation.", 410)
        snapshot_directory = store.snapshots / str(status["snapshot_id"])
        for index, entry in enumerate(self.manifest["files"]):
            data = store._read_private_file(
                snapshot_directory / "objects" / f"{index:04d}",
                store.limits.max_file_bytes,
            )
            if (
                len(data) != entry["size_bytes"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise DistributedError("snapshot_integrity_error", "Committed snapshot object failed integrity validation.", 410)
        if not self.root.exists():
            files: list[tuple[dict[str, object], bytes]] = []
            for index, entry in enumerate(self.manifest["files"]):
                data = store._read_private_file(
                    snapshot_directory / "objects" / f"{index:04d}",
                    store.limits.max_file_bytes,
                )
                if (
                    len(data) != entry["size_bytes"]
                    or hashlib.sha256(data).hexdigest() != entry["sha256"]
                ):
                    raise DistributedError("snapshot_integrity_error", "Committed snapshot object failed integrity validation.", 410)
                files.append((entry, data))
            store._materialize_view(str(status["snapshot_id"]), self.manifest, files)
        store._verify_source_view(self.root, self.manifest)
        self.index = RepositoryIndex(self.root)
        self._repository_index_type = RepositoryIndex

    def verify_indexed_sources(self) -> dict[str, object]:
        verified = 0
        for entry in self.manifest["files"]:
            try:
                data = self.index.read_file_bytes(
                    entry["path"], max_bytes=self.store.limits.max_file_bytes,
                )
            except OSError:
                continue
            if data is None:
                continue
            if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise DistributedError("snapshot_integrity_error", "Repository index observed source bytes inconsistent with the manifest.", 410)
            verified += 1
        return {"verified_files": verified, "manifest_files": len(self.manifest["files"])}

    def query(self, operation: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        self.verify_indexed_sources()
        return self.index.query(operation, arguments or {})

    def build_context(self, task: str):
        from synai.coding_agent.context import ContextEngine, ContextRequest

        self.verify_indexed_sources()
        return ContextEngine().build(ContextRequest(task, self.index))


def _require_id(value: str) -> None:
    if not isinstance(value, str) or len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
        raise DistributedError("upload_not_found", "Snapshot upload was not found.", 404)
